from typing import Optional
from shioaji_const.account import BaseAccount
from shioaji_const.base import BaseModel


class TraceLog(BaseModel):
    person_id: str
    account: Optional[BaseAccount]
    ip: str
    action: str
    func: str
    func_content: dict
    result: str
