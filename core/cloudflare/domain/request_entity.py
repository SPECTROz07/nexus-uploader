from dataclasses import dataclass
#from core.__seedwork.domain.entities import Entity

@dataclass
class Request:
    user_agent: dict
    cloudflare_cookie_value: dict

    @classmethod
    def from_dict(cls, user_agent: dict, cloudflare_cookie_value: dict):
        return cls(user_agent, cloudflare_cookie_value)