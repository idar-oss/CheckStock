from pydantic import BaseModel, Field


class WarehouseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ReceiptCreate(BaseModel):
    product_id: int = Field(gt=0)
    warehouse_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000000)


class TransferCreate(BaseModel):
    product_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    destination_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000000)


class ShipmentCreate(BaseModel):
    product_id: int = Field(gt=0)
    warehouse_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000000)
    reference: str = Field(default='', max_length=100)
