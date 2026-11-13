"""Transactional stock transitions. All balance changes go through this module."""
from datetime import datetime, timezone
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .models import ActionRequest, Balance, Journal, Product, Shipment, Transfer, Warehouse


def fail(code, message, status=409, **data):
    raise HTTPException(status_code=status, detail={'code': code, 'message': message, **data})


def entity(db, model, identifier, active=False):
    obj = db.get(model, identifier)
    if obj is None:
        fail('not_found', 'Объект не найден', 404)
    if active and not obj.active:
        fail('archived_warehouse', 'Склад архивирован; новые операции запрещены')
    return obj


def locked_balance(db, warehouse_id, product_id):
    """Create zero row if needed, then lock it; on PostgreSQL UPSERT protects races."""
    vals = dict(warehouse_id=warehouse_id, product_id=product_id, on_hand=0, reserved=0)
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
        db.execute(insert(Balance).values(**vals).on_conflict_do_nothing(index_elements=['warehouse_id', 'product_id']))
    elif db.bind.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(Balance).values(**vals).on_conflict_do_nothing(index_elements=['warehouse_id', 'product_id']))
    else:
        existing = db.scalar(select(Balance.id).where(Balance.warehouse_id == warehouse_id, Balance.product_id == product_id))
        if existing is None:
            db.add(Balance(**vals))
            db.flush()
    return db.scalar(select(Balance).where(Balance.warehouse_id == warehouse_id, Balance.product_id == product_id).with_for_update())


def availability(db, product_id, warehouse_id, needed):
    b = locked_balance(db, warehouse_id, product_id)
    available = b.on_hand - b.reserved
    if available < needed:
        alternatives = db.execute(select(Warehouse.name, (Balance.on_hand - Balance.reserved)).join(Balance, Balance.warehouse_id == Warehouse.id).where(Balance.product_id == product_id, Warehouse.id != warehouse_id, Warehouse.active.is_(True), Balance.on_hand > Balance.reserved)).all()
        fail('insufficient_stock', 'Недостаточно доступного товара на выбранном складе', available=available, requested=needed, alternatives=[{'warehouse': n, 'available': a} for n, a in alternatives])
    return b


def event(db, event_type, product_id=None, source=None, dest=None, qty=0, details=''):
    db.add(Journal(event_type=event_type, product_id=product_id, warehouse_from_id=source, warehouse_to_id=dest, quantity=qty, details=details))


def action(db: Session, key: str, kind: str, callback, payload=None):
    # Idempotency applies to the exact action AND its request data.
    # Reusing a key with another quantity must not silently return an old result.
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    identity = f'{kind}|{fingerprint}'
    if not key or len(key) > 100:
        fail('invalid_key', 'Нужен корректный ключ операции', 400)
    try:
        with db.begin():
            req = ActionRequest(key=key, action=identity)
            db.add(req)
            db.flush()  # unique key claimed *before* any stock changes
            result = callback()
            db.flush()
            req.result = result
        return result
    except IntegrityError:
        db.rollback()
        existing = db.get(ActionRequest, key)
        if existing is not None:
            if existing.action != identity:
                fail('idempotency_conflict', 'Ключ уже использован для другой операции или других данных')
            return existing.result
        fail('integrity_conflict', 'Конфликт данных; повторите операцию')


def seed(db):
    with db.begin():
        if db.scalar(select(func.count()).select_from(Product)):
            return
        warehouses = [Warehouse(name='Север'), Warehouse(name='Юг')]
        products = [Product(sku='SKU-101', name='Органайзер'), Product(sku='SKU-102', name='Контейнер'), Product(sku='SKU-103', name='Корзина')]
        db.add_all(warehouses + products)
        db.flush()
        for p, q1, q2 in zip(products, [10, 5, 0], [6, 0, 4]):
            for w, qty in zip(warehouses, (q1, q2)):
                db.add(Balance(product_id=p.id, warehouse_id=w.id, on_hand=qty, reserved=0))
                if qty:
                    event(db, 'initial_receipt', p.id, dest=w.id, qty=qty, details='Начальный тестовый приход')


def receipt(db, data):
    entity(db, Product, data.product_id)
    entity(db, Warehouse, data.warehouse_id, active=True)
    b = locked_balance(db, data.warehouse_id, data.product_id)
    b.on_hand += data.quantity
    event(db, 'receipt', data.product_id, dest=data.warehouse_id, qty=data.quantity)
    return {'message': 'Приход проведён', 'quantity': data.quantity}


def transfer_create(db, data):
    if data.source_id == data.destination_id:
        fail('same_warehouse', 'Выберите разные склады', 422)
    entity(db, Product, data.product_id)
    entity(db, Warehouse, data.source_id, active=True)
    entity(db, Warehouse, data.destination_id, active=True)
    b = availability(db, data.product_id, data.source_id, data.quantity)
    b.on_hand -= data.quantity
    t = Transfer(product_id=data.product_id, source_id=data.source_id, destination_id=data.destination_id, quantity=data.quantity)
    db.add(t)
    db.flush()
    event(db, 'transfer_sent', data.product_id, data.source_id, data.destination_id, data.quantity, f'Перемещение #{t.id} · в пути')
    return {'id': t.id, 'status': t.status, 'message': 'Перемещение отправлено'}


def transfer_receive(db, identifier):
    t = db.scalar(select(Transfer).where(Transfer.id == identifier).with_for_update())
    if not t:
        fail('not_found', 'Перемещение не найдено', 404)
    if t.status == 'received':
        return {'id': t.id, 'status': t.status, 'message': 'Уже принято (повторно не оприходовано)'}
    b = locked_balance(db, t.destination_id, t.product_id)
    b.on_hand += t.quantity
    t.status = 'received'
    t.received_at = datetime.now(timezone.utc)
    event(db, 'transfer_received', t.product_id, t.source_id, t.destination_id, t.quantity, f'Перемещение #{t.id} · приёмка')
    return {'id': t.id, 'status': t.status, 'message': 'Перемещение принято'}


def fbs_reserve(db, data):
    entity(db, Product, data.product_id)
    entity(db, Warehouse, data.warehouse_id, active=True)
    b = availability(db, data.product_id, data.warehouse_id, data.quantity)
    b.reserved += data.quantity
    s = Shipment(kind='FBS', product_id=data.product_id, warehouse_id=data.warehouse_id, quantity=data.quantity, status='reserved', reference=data.reference.strip())
    db.add(s)
    db.flush()
    event(db, 'fbs_reserved', data.product_id, source=data.warehouse_id, qty=data.quantity, details=f'FBS #{s.id} · резерв')
    return {'id': s.id, 'status': s.status, 'message': 'FBS зарезервирован'}


def shipment_transition(db, identifier, kind, transition):
    s = db.scalar(select(Shipment).where(Shipment.id == identifier).with_for_update())
    if not s or s.kind != kind:
        fail('not_found', 'Отправка не найдена', 404)
    if kind == 'FBS':
        if transition == 'ship':
            target, allowed = 'in_transit', 'reserved'
        elif transition == 'cancel':
            target, allowed = 'cancelled', 'reserved'
        else:
            target, allowed = 'delivered', 'in_transit'
    else:
        target, allowed = 'received', 'in_transit'
    if s.status == target:
        return {'id': s.id, 'status': s.status, 'message': 'Уже подтверждено; повторного списания нет'}
    if s.status != allowed:
        fail('invalid_transition', f'Нельзя выполнить действие из статуса {s.status}')
    if kind == 'FBS' and transition in ('ship', 'cancel'):
        b = locked_balance(db, s.warehouse_id, s.product_id)
        b.reserved -= s.quantity
        if transition == 'ship':
            b.on_hand -= s.quantity
    s.status = target
    event_name = {'ship': 'fbs_sent', 'cancel': 'fbs_cancelled', 'deliver': 'fbs_delivered', 'receive': 'fbo_received'}[transition]
    event(db, event_name, s.product_id, source=s.warehouse_id, qty=s.quantity, details=f'{kind} #{s.id} · {target}')
    return {'id': s.id, 'status': s.status, 'message': 'Статус обновлён'}


def fbo_ship(db, data):
    entity(db, Product, data.product_id)
    entity(db, Warehouse, data.warehouse_id, active=True)
    b = availability(db, data.product_id, data.warehouse_id, data.quantity)
    b.on_hand -= data.quantity
    s = Shipment(kind='FBO', product_id=data.product_id, warehouse_id=data.warehouse_id, quantity=data.quantity, status='in_transit', reference=data.reference.strip())
    db.add(s)
    db.flush()
    event(db, 'fbo_sent', data.product_id, source=data.warehouse_id, qty=data.quantity, details=f'FBO #{s.id} · едет на маркетплейс')
    return {'id': s.id, 'status': s.status, 'message': 'FBO отправлен'}
