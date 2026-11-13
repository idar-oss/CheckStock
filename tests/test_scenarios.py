import uuid

def post(c, route, payload=None, key=None):
    return c.post(route, json=payload, headers={'Idempotency-Key': key or str(uuid.uuid4())})

def rows(c):
    return c.get('/api/dashboard').json()

def stock(c, sku, warehouse):
    return next(r for r in rows(c)['inventory'] if r['sku']==sku and r['warehouse']==warehouse)

def test_initial_seed_and_health(client):
    d=rows(client)
    assert client.get('/health').json()['status']=='ok'
    assert [(stock(client,'SKU-101',w)['on_hand']) for w in ('Север','Юг')]==[10,6]
    assert stock(client,'SKU-102','Север')['on_hand']==5
    assert stock(client,'SKU-103','Юг')['on_hand']==4
    assert d['stats']['physical']==25
    assert client.get('/').status_code==200

def test_receipt_and_idempotent_create(client):
    payload={'product_id':1,'warehouse_id':1,'quantity':7}
    key='receipt-retry-01'
    assert post(client,'/api/receipts',payload,key).status_code==200
    assert post(client,'/api/receipts',payload,key).status_code==200
    assert stock(client,'SKU-101','Север')['on_hand']==17
    assert len([x for x in rows(client)['history'] if x['type']=='receipt'])==1

def test_reserve_ship_deliver_and_repeat(client):
    obj={'product_id':1,'warehouse_id':1,'quantity':4,'reference':'order-42'}
    s=post(client,'/api/fbs/reserve',obj).json()['id']
    assert stock(client,'SKU-101','Север')['on_hand']==10
    assert stock(client,'SKU-101','Север')['reserved']==4
    assert stock(client,'SKU-101','Север')['available']==6
    assert post(client,f'/api/fbs/{s}/ship').status_code==200
    assert post(client,f'/api/fbs/{s}/ship').status_code==200
    assert stock(client,'SKU-101','Север')['on_hand']==6
    assert stock(client,'SKU-101','Север')['reserved']==0
    assert rows(client)['stats']['fbs_transit']==4
    assert post(client,f'/api/fbs/{s}/deliver').status_code==200
    assert rows(client)['stats']['fbs_transit']==0
    assert post(client,f'/api/fbs/{s}/cancel').status_code==409

def test_cancel_reservation(client):
    s=post(client,'/api/fbs/reserve',{'product_id':1,'warehouse_id':1,'quantity':3}).json()['id']
    assert post(client,f'/api/fbs/{s}/cancel').status_code==200
    assert post(client,f'/api/fbs/{s}/cancel').status_code==200
    assert stock(client,'SKU-101','Север')['on_hand']==10
    assert stock(client,'SKU-101','Север')['available']==10
    assert post(client,f'/api/fbs/{s}/ship').status_code==409

def test_transfer_and_double_receive(client):
    p={'product_id':1,'source_id':1,'destination_id':2,'quantity':4}
    t=post(client,'/api/transfers',p).json()['id']
    assert stock(client,'SKU-101','Север')['on_hand']==6
    assert stock(client,'SKU-101','Юг')['on_hand']==6
    assert rows(client)['stats']['transfer_transit']==4
    assert post(client,f'/api/transfers/{t}/receive').status_code==200
    assert post(client,f'/api/transfers/{t}/receive').status_code==200
    assert stock(client,'SKU-101','Юг')['on_hand']==10
    assert rows(client)['stats']['transfer_transit']==0
    assert len([j for j in rows(client)['history'] if j['type']=='transfer_received'])==1

def test_fbo_transit_and_receive(client):
    s=post(client,'/api/fbo/ship',{'product_id':2,'warehouse_id':1,'quantity':3}).json()['id']
    assert stock(client,'SKU-102','Север')['on_hand']==2
    assert rows(client)['stats']['fbo_transit']==3
    assert post(client,f'/api/fbo/{s}/receive').status_code==200
    assert post(client,f'/api/fbo/{s}/receive').status_code==200
    assert rows(client)['stats']['fbo_transit']==0
    assert stock(client,'SKU-102','Север')['on_hand']==2

def test_insufficient_local_despite_other_warehouse_stock(client):
    r=post(client,'/api/fbs/reserve',{'product_id':1,'warehouse_id':1,'quantity':11})
    assert r.status_code==409
    assert r.json()['detail']['code']=='insufficient_stock'
    assert {'warehouse':'Юг','available':6} in r.json()['detail']['alternatives']
    assert stock(client,'SKU-101','Север')['on_hand']==10
    assert post(client,'/api/fbo/ship',{'product_id':3,'warehouse_id':1,'quantity':1}).status_code==409

def test_archive_keeps_stock_history_and_inflight_receive(client):
    t=post(client,'/api/transfers',{'product_id':1,'source_id':1,'destination_id':2,'quantity':2}).json()['id']
    r=client.delete('/api/warehouses/2')
    assert r.status_code==200
    assert stock(client,'SKU-101','Юг')['on_hand']==6
    assert stock(client,'SKU-101','Юг')['active'] is False
    assert post(client,'/api/receipts',{'product_id':1,'warehouse_id':2,'quantity':1}).status_code==409
    assert post(client,f'/api/transfers/{t}/receive').status_code==200
    assert stock(client,'SKU-101','Юг')['on_hand']==8
    assert any(j['type']=='warehouse_archived' for j in rows(client)['history'])

def test_invalid_transfer_and_warehouse_management(client):
    assert post(client,'/api/transfers',{'product_id':1,'source_id':1,'destination_id':1,'quantity':2}).status_code==422
    assert post(client,'/api/receipts',{'product_id':1,'warehouse_id':1,'quantity':-3}).status_code==422
    w=post(client,'/api/warehouses',{'name':'Восток'}).json()['id']
    assert client.patch(f'/api/warehouses/{w}',json={'name':'Восток новый'}).status_code==200
    assert any(i['warehouse']=='Восток новый' for i in rows(client)['inventory'])

def test_same_idempotency_key_different_route(client):
    key='shared-key'
    assert post(client,'/api/receipts',{'product_id':1,'warehouse_id':1,'quantity':1},key).status_code==200
    r=post(client,'/api/fbs/reserve',{'product_id':1,'warehouse_id':1,'quantity':1},key)
    assert r.status_code==409
    assert stock(client,'SKU-101','Север')['on_hand']==11
    assert stock(client,'SKU-101','Север')['reserved']==0


def test_same_key_with_different_quantities_is_a_conflict(client):
    key = 'one-operation-only'
    first = {'product_id': 1, 'warehouse_id': 1, 'quantity': 2}
    second = {'product_id': 1, 'warehouse_id': 1, 'quantity': 3}
    assert post(client, '/api/receipts', first, key).status_code == 200
    response = post(client, '/api/receipts', second, key)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'idempotency_conflict'
    assert stock(client, 'SKU-101', 'Север')['on_hand'] == 12


def test_reserve_blocks_fbo_and_transfer_from_same_ff(client):
    r = post(client, '/api/fbs/reserve', {'product_id': 2, 'warehouse_id': 1, 'quantity': 4})
    assert r.status_code == 200
    assert post(client, '/api/fbo/ship', {'product_id': 2, 'warehouse_id': 1, 'quantity': 2}).status_code == 409
    assert post(client, '/api/transfers', {
        'product_id': 2, 'source_id': 1, 'destination_id': 2, 'quantity': 2
    }).status_code == 409
    row = stock(client, 'SKU-102', 'Север')
    assert (row['on_hand'], row['reserved'], row['available']) == (5, 4, 1)


def test_full_chain_and_separate_transit_totals(client):
    # Arrival +3, transfer 4, FBS 2 and FBO 3; all operate on the same SKU.
    assert post(client, '/api/receipts', {'product_id': 1, 'warehouse_id': 1, 'quantity': 3}).status_code == 200
    tr = post(client, '/api/transfers', {'product_id': 1, 'source_id': 1, 'destination_id': 2, 'quantity': 4}).json()['id']
    fbs = post(client, '/api/fbs/reserve', {'product_id': 1, 'warehouse_id': 1, 'quantity': 2}).json()['id']
    fbo = post(client, '/api/fbo/ship', {'product_id': 1, 'warehouse_id': 1, 'quantity': 3}).json()['id']
    assert post(client, f'/api/fbs/{fbs}/ship').status_code == 200
    state = rows(client)
    assert state['stats']['transfer_transit'] == 4
    assert state['stats']['fbo_transit'] == 3
    assert state['stats']['fbs_transit'] == 2
    assert stock(client, 'SKU-101', 'Север')['on_hand'] == 4
    assert stock(client, 'SKU-101', 'Север')['reserved'] == 0
    assert post(client, f'/api/transfers/{tr}/receive').status_code == 200
    assert post(client, f'/api/fbo/{fbo}/receive').status_code == 200
    assert post(client, f'/api/fbs/{fbs}/deliver').status_code == 200
    assert stock(client, 'SKU-101', 'Юг')['on_hand'] == 10
    assert rows(client)['stats'] == {
        'physical': 23, 'available': 23, 'reserved': 0,
        'transfer_transit': 0, 'fbo_transit': 0, 'fbs_transit': 0,
    }


def test_archive_does_not_delete_reserved_stock(client):
    fbs = post(client, '/api/fbs/reserve', {'product_id': 1, 'warehouse_id': 1, 'quantity': 5}).json()['id']
    assert client.delete('/api/warehouses/1').status_code == 200
    assert stock(client, 'SKU-101', 'Север')['reserved'] == 5
    assert post(client, f'/api/fbs/{fbs}/cancel').status_code == 200
    assert stock(client, 'SKU-101', 'Север')['on_hand'] == 10
    assert stock(client, 'SKU-101', 'Север')['reserved'] == 0
    assert post(client, '/api/fbo/ship', {'product_id': 1, 'warehouse_id': 1, 'quantity': 1}).status_code == 409


def test_history_shows_real_destinations_for_shipments(client):
    fbs = post(client, '/api/fbs/reserve', {'product_id': 1, 'warehouse_id': 1, 'quantity': 1}).json()['id']
    assert post(client, f'/api/fbs/{fbs}/ship').status_code == 200
    fbo = post(client, '/api/fbo/ship', {'product_id': 2, 'warehouse_id': 1, 'quantity': 1}).json()['id']
    assert post(client, f'/api/fbo/{fbo}/receive').status_code == 200
    history = rows(client)['history']
    assert next(x for x in history if x['type'] == 'fbs_sent')['destination'] == 'Клиент'
    assert next(x for x in history if x['type'] == 'fbo_sent')['destination'] == 'Маркетплейс'
