# app/engines/comfy_template.py
import copy


def _sub(value, injections: dict):
    if isinstance(value, dict):
        return {k: _sub(v, injections) for k, v in value.items()}
    if isinstance(value, list):
        return [_sub(v, injections) for v in value]
    if isinstance(value, str):
        for token, replacement in injections.items():
            if value == token:
                return replacement            # 整串匹配：保留原始类型（int 等）
            if token in value:
                value = value.replace(token, str(replacement))
    return value


def render_workflow(template: dict, injections: dict[str, str | int]) -> dict:
    return _sub(copy.deepcopy(template), injections)
