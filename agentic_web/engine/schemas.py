from typing import Any, List, Literal
from pydantic import BaseModel, Field, model_validator


class Parameter(BaseModel):
    name: str
    description: str = ''
    type: Literal['string'] = 'string'


class Step(BaseModel):
    action: Literal['click', 'fill', 'press', 'select', 'check', 'uncheck', 'wait']
    selector: str
    value: str = ''

    @model_validator(mode='before')
    @classmethod
    def _compat_step(cls, data: Any) -> Any:
        if isinstance(data, dict) and 'action' not in data and 'op' in data:
            op_map = {
                'fill': 'fill',
                'select': 'select',
                'click': 'click',
                'submit': 'click',
                'click_item_button': 'click',
                'read_state': 'wait',
            }
            d = dict(data)
            d['action'] = op_map.get(str(d.get('op', '')), 'click')
            d.setdefault('selector', d.get('form_selector') or d.get('list_selector') or 'body')
            d.setdefault('value', '')
            return {k: d[k] for k in ('action', 'selector', 'value')}
        return data


class Tool(BaseModel):
    name: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,63}$')
    description: str
    parameters: List[Parameter] = Field(default_factory=list)
    steps: List[Step] = Field(min_length=1, max_length=25)

    @model_validator(mode='before')
    @classmethod
    def _compat_tool(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get('parameters'), dict):
            props = data['parameters'].get('properties', {})
            d = dict(data)
            d['parameters'] = [
                {'name': k, 'description': str(v.get('description', '')), 'type': 'string'}
                for k, v in props.items()
            ]
            return {k: d[k] for k in ('name', 'description', 'parameters', 'steps') if k in d}
        return data


class Synthesis(BaseModel):
    tools: List[Tool] = Field(min_length=1, max_length=20)


class Argument(BaseModel):
    name: str
    value: str


class Decision(BaseModel):
    kind: Literal['call', 'done', 'input', 'login', 'expand']
    tool_name: str = ''
    arguments: List[Argument] = Field(default_factory=list)
    message: str


class Verdict(BaseModel):
    status: Literal['success', 'failure', 'input', 'login']
    reason: str
    evidence: List[str] = Field(default_factory=list)


class WorkflowDecision(BaseModel):
    kind: Literal['execute', 'input', 'done']
    url: str = ''
    task: str = ''
    message: str
