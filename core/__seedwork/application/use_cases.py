# core/__seedwork/application/use_cases.py
from abc import ABC, abstractmethod

class UseCase(ABC):
    @abstractmethod 
    def execute(self, *args, **kwargs):
        raise NotImplementedError