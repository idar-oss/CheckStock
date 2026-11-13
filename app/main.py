import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import get_db, engine, SessionLocal
from .models import Base, Balance, Journal, Product, Shipment, Transfer, Warehouse
from .schemas import ReceiptCreate, ShipmentCreate, TransferCreate, WarehouseCreate
from . import service

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app):
    # Docker runs Alembic before starting the app. Local SQLite uses create_all.
    if engine.dialect.name == 'sqlite':
        Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        with SessionLocal() as db:
            service.seed(db)
    yield


app = FastAPI(title='CheckStock', version='1.0.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'templates')


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, 'index.html')


@app.get('/health')
def health():
    return {'status': 'ok'}


def key_header(idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    # For clients that don't supply a key, create a fresh one.
    return idempotency_key or str(uuid.uuid4())


def apply(db, key, kind, callback, payload=None):
    return service.action(db, key, kind, callback, payload.model_dump() if payload is not None else None)


@app.get('/api/dashboard')
def dashboard(db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    balances = db.scalars(select(Balance)).all()
    transfers = db.scalars(select(Transfer).order_by(Transfer.id.desc())).all()
    shipments = db.scalars(select(Shipment).order_by(Shipment.id.desc())).all()
    journal = db.scalars(select(Journal).order_by(Journal.id.desc())).all()
    prod_by = {p.id: p for p in products}
    wh_by = {w.id: w for w in warehouses}
    bmap = {(b.product_id, b.warehouse_id): b for b in balances}
    fmt = lambda dt: dt.isoformat() if dt else None
    inventory = []
    for p in products:
        for w in warehouses:
            b = bmap.get((p.id, w.id))
            on_hand, reserved = (b.on_hand, b.reserved) if b else (0, 0)
            inventory.append({'product_id': p.id, 'sku': p.sku, 'product': p.name, 'warehouse_id': w.id, 'warehouse': w.name, 'active': w.active, 'on_hand': on_hand, 'reserved': reserved, 'available': on_hand-reserved})
    movement_rows = [{'id': t.id, 'sku': prod_by[t.product_id].sku, 'product': prod_by[t.product_id].name, 'source': wh_by[t.source_id].name, 'destination': wh_by[t.destination_id].name, 'quantity': t.quantity, 'status': t.status, 'created_at': fmt(t.created_at)} for t in transfers]
    shipment_rows = [{'id': s.id, 'kind': s.kind, 'sku': prod_by[s.product_id].sku, 'product': prod_by[s.product_id].name, 'warehouse': wh_by[s.warehouse_id].name, 'quantity': s.quantity, 'reference': s.reference, 'status': s.status, 'created_at': fmt(s.created_at)} for s in shipments]
    history = [{'id': j.id, 'type': j.event_type, 'sku': prod_by[j.product_id].sku if j.product_id else '—', 'product': prod_by[j.product_id].name if j.product_id else '—', 'source': wh_by[j.warehouse_from_id].name if j.warehouse_from_id else '—', 'destination': wh_by[j.warehouse_to_id].name if j.warehouse_to_id else {'fbs_reserved': 'Резерв FBS', 'fbs_cancelled': 'Доступный остаток', 'fbs_sent': 'Клиент', 'fbs_delivered': 'Клиент', 'fbo_sent': 'Маркетплейс', 'fbo_received': 'Маркетплейс'}.get(j.event_type, '—'), 'quantity': j.quantity, 'details': j.details, 'created_at': fmt(j.created_at)} for j in journal]
    return {'products': [{'id': p.id, 'sku': p.sku, 'name': p.name} for p in products], 'warehouses': [{'id': w.id, 'name': w.name, 'active': w.active} for w in warehouses], 'inventory': inventory, 'transfers': movement_rows, 'shipments': shipment_rows, 'history': history, 'stats': {'physical': sum(b.on_hand for b in balances), 'available': sum(b.on_hand-b.reserved for b in balances), 'reserved': sum(b.reserved for b in balances), 'transfer_transit': sum(t.quantity for t in transfers if t.status == 'in_transit'), 'fbo_transit': sum(s.quantity for s in shipments if s.kind == 'FBO' and s.status == 'in_transit'), 'fbs_transit': sum(s.quantity for s in shipments if s.kind == 'FBS' and s.status == 'in_transit')}}


@app.post('/api/warehouses')
def create_warehouse(data: WarehouseCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    def work():
        name = data.name.strip()
        if not name:
            service.fail('invalid_name', 'Название обязательно', 422)
        if db.scalar(select(Warehouse).where(Warehouse.name == name)):
            service.fail('duplicate_name', 'Название склада уже используется')
        w = Warehouse(name=name)
        db.add(w)
        db.flush()
        service.event(db, 'warehouse_created', details=f'Создан склад «{name}»')
        return {'id': w.id, 'message': 'Склад добавлен'}
    return apply(db, key, 'warehouse_create', work, data)


@app.patch('/api/warehouses/{warehouse_id}')
def rename_warehouse(warehouse_id: int, data: WarehouseCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    def work():
        w = service.entity(db, Warehouse, warehouse_id, active=True)
        name = data.name.strip()
        if not name:
            service.fail('invalid_name', 'Название обязательно', 422)
        if db.scalar(select(Warehouse).where(Warehouse.name == name, Warehouse.id != warehouse_id)):
            service.fail('duplicate_name', 'Название склада уже используется')
        old = w.name
        w.name = name
        service.event(db, 'warehouse_renamed', details=f'Склад «{old}» → «{name}»')
        return {'id': w.id, 'message': 'Склад переименован'}
    return apply(db, key, f'warehouse_rename:{warehouse_id}', work, data)


@app.delete('/api/warehouses/{warehouse_id}')
def archive_warehouse(warehouse_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    def work():
        w = service.entity(db, Warehouse, warehouse_id)
        if w.active:
            w.active = False
            service.event(db, 'warehouse_archived', details=f'Склад «{w.name}» архивирован; остатки и история сохранены')
        return {'id': w.id, 'message': 'Склад архивирован; товары продолжают числиться за ним'}
    return apply(db, key, f'warehouse_archive:{warehouse_id}', work)


@app.post('/api/receipts')
def receipt(data: ReceiptCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, 'receipt', lambda: service.receipt(db, data), data)


@app.post('/api/transfers')
def transfer(data: TransferCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, 'transfer_create', lambda: service.transfer_create(db, data), data)


@app.post('/api/transfers/{transfer_id}/receive')
def receive_transfer(transfer_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, f'transfer_receive:{transfer_id}', lambda: service.transfer_receive(db, transfer_id))


@app.post('/api/fbs/reserve')
def reserve_fbs(data: ShipmentCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, 'fbs_reserve', lambda: service.fbs_reserve(db, data), data)


@app.post('/api/fbs/{shipment_id}/ship')
def send_fbs(shipment_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, f'fbs_ship:{shipment_id}', lambda: service.shipment_transition(db, shipment_id, 'FBS', 'ship'))


@app.post('/api/fbs/{shipment_id}/cancel')
def cancel_fbs(shipment_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, f'fbs_cancel:{shipment_id}', lambda: service.shipment_transition(db, shipment_id, 'FBS', 'cancel'))


@app.post('/api/fbs/{shipment_id}/deliver')
def deliver_fbs(shipment_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, f'fbs_deliver:{shipment_id}', lambda: service.shipment_transition(db, shipment_id, 'FBS', 'deliver'))


@app.post('/api/fbo/ship')
def send_fbo(data: ShipmentCreate, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, 'fbo_ship', lambda: service.fbo_ship(db, data), data)


@app.post('/api/fbo/{shipment_id}/receive')
def receive_fbo(shipment_id: int, db: Session = Depends(get_db), key: str = Depends(key_header)):
    return apply(db, key, f'fbo_receive:{shipment_id}', lambda: service.shipment_transition(db, shipment_id, 'FBO', 'receive'))
