# core/__seedwork/domain/entities.py
from dataclasses import dataclass, field
from uuid import UUID, uuid4

@dataclass
class Entity:
    unique_entity_id: UUID = field(default_factory=uuid4)