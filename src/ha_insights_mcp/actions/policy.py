"""Action policy: which services the assistant may call, with which values, at which risk.

Pure: inputs are the policy, the requested action, current entity states and (optionally)
HA's service schemas; the output is a decision with one check per rule. No I/O, no MCP.

Everything not listed in the policy file is denied. Value bounds are the intersection of
three sources: the policy, the entity's own limits (e.g. a thermostat's min_temp) and the
selector in HA's service schema.
"""

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ha_insights_mcp.ha.models import EntityState

Risk = Literal["low", "medium", "high"]
RISK_LEVELS: tuple[Risk, ...] = ("low", "medium", "high")

SERVICE_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Never allowed, whatever the policy file says: these control HA itself, run arbitrary
# code, or replay other actions that would bypass this policy.
FORBIDDEN_DOMAINS = frozenset(
    {
        "automation",
        "backup",
        "hassio",
        "homeassistant",
        "logger",
        "pyscript",
        "python_script",
        "recorder",
        "rest_command",
        "scene",
        "script",
        "shell_command",
        "system_log",
        "update",
    }
)

# (domain, field) -> (min attribute, max attribute) on the target entity.
ATTRIBUTE_BOUNDS: dict[tuple[str, str], tuple[str, str]] = {
    ("climate", "temperature"): ("min_temp", "max_temp"),
    ("climate", "target_temp_low"): ("min_temp", "max_temp"),
    ("climate", "target_temp_high"): ("min_temp", "max_temp"),
    ("climate", "humidity"): ("min_humidity", "max_humidity"),
    ("light", "color_temp_kelvin"): ("min_color_temp_kelvin", "max_color_temp_kelvin"),
    ("water_heater", "temperature"): ("min_temp", "max_temp"),
    ("number", "value"): ("min", "max"),
    ("input_number", "value"): ("min", "max"),
}

# (domain, field) -> attribute listing the values the entity accepts.
ATTRIBUTE_OPTIONS: dict[tuple[str, str], str] = {
    ("climate", "hvac_mode"): "hvac_modes",
    ("climate", "fan_mode"): "fan_modes",
    ("climate", "preset_mode"): "preset_modes",
    ("fan", "preset_mode"): "preset_modes",
    ("select", "option"): "options",
    ("media_player", "source"): "source_list",
}


class PolicyError(ValueError):
    """The policy file is missing or invalid."""


# ------------------------------------------------------------------ policy file


class _PolicyModel(BaseModel):
    # extra="forbid": a typo in a security config must fail loudly, not be ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")


class DomainPolicy(_PolicyModel):
    risk: Risk
    services: list[str] = Field(min_length=1)
    # Service data fields the assistant may pass; anything else is rejected.
    fields: list[str] = []
    bounds: dict[str, tuple[float, float]] = {}
    # Per-service override of the domain risk, e.g. lock.unlock = "high".
    service_risk: dict[str, Risk] = {}

    @model_validator(mode="after")
    def _consistent(self) -> "DomainPolicy":
        for name in (*self.services, *self.fields):
            if not _NAME_RE.fullmatch(name):
                raise ValueError(f"invalid service or field name: {name!r}")
        for name, (low, high) in self.bounds.items():
            if name not in self.fields:
                raise ValueError(f"bounds for {name!r}, which is not in fields")
            if low > high:
                raise ValueError(f"bounds for {name!r}: min is greater than max")
        unknown = set(self.service_risk) - set(self.services)
        if unknown:
            raise ValueError(f"service_risk for services not allowed: {sorted(unknown)}")
        return self


class PolicyDefaults(_PolicyModel):
    max_entities: int = Field(default=10, ge=1, le=50)
    # More target entities than this raises the risk by one level.
    bulk_threshold: int = Field(default=3, ge=1)
    rate_limit_per_minute: int = Field(default=10, ge=1)


class DenyList(_PolicyModel):
    entities: list[str] = []


class Policy(_PolicyModel):
    defaults: PolicyDefaults = PolicyDefaults()
    domains: dict[str, DomainPolicy] = {}
    deny: DenyList = DenyList()

    @model_validator(mode="after")
    def _no_forbidden_domains(self) -> "Policy":
        forbidden = sorted(set(self.domains) & FORBIDDEN_DOMAINS)
        if forbidden:
            raise ValueError(f"domains that can never be allowed: {forbidden}")
        for name in self.domains:
            if not _NAME_RE.fullmatch(name):
                raise ValueError(f"invalid domain name: {name!r}")
        return self

    def allows(self, service: str) -> bool:
        domain, _, name = service.partition(".")
        rules = self.domains.get(domain)
        return rules is not None and name in rules.services

    def risk_of(self, service: str) -> Risk:
        domain, _, name = service.partition(".")
        rules = self.domains[domain]
        return rules.service_risk.get(name, rules.risk)


def load_policy(path: Path) -> Policy:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"action policy file not found: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"cannot read action policy {path}: {exc}") from None
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'policy'}: {e['msg']}" for e in exc.errors()
        )
        raise PolicyError(f"invalid action policy {path}: {problems}") from None


# --------------------------------------------------------------------- evaluation


@dataclass(frozen=True, slots=True)
class ActionRequest:
    service: str  # "domain.service"
    entity_ids: list[str]
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.service.partition(".")[0]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    risk: Risk | None  # None when the service is not in the policy at all
    needs_confirmation: bool
    checks: list[Check]
    # Bounds for every bounded field of the domain, intersected over all targets.
    effective_bounds: dict[str, tuple[float, float]]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def raise_risk(risk: Risk) -> Risk:
    return RISK_LEVELS[min(RISK_LEVELS.index(risk) + 1, len(RISK_LEVELS) - 1)]


def schema_fields(
    services: Mapping[str, Any], domain: str, service: str
) -> dict[str, dict[str, Any]] | None:
    """Flattened field specs of one service from HA `get_services`; None if unknown.

    HA groups some fields into collapsible sections (`{"fields": {...}}`), e.g. the
    advanced options of light.turn_on.
    """
    spec = services.get(domain, {}).get(service)
    if spec is None:
        return None
    flat: dict[str, dict[str, Any]] = {}
    for name, value in (spec.get("fields") or {}).items():
        if isinstance(value, dict) and isinstance(value.get("fields"), dict):
            flat.update(value["fields"])
        else:
            flat[name] = value or {}
    return flat


def selector_bounds(spec: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """min/max from a field selector such as {"number": {"min": 0, "max": 100}}."""
    for options in (spec.get("selector") or {}).values():
        if isinstance(options, dict) and ("min" in options or "max" in options):
            return _num(options.get("min")), _num(options.get("max"))
    return None, None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _intersect(
    sources: list[tuple[float | None, float | None]],
) -> tuple[float, float] | None:
    lows = [low for low, _ in sources if low is not None]
    highs = [high for _, high in sources if high is not None]
    if not lows and not highs:
        return None
    return (max(lows) if lows else -math.inf, min(highs) if highs else math.inf)


def _fmt(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _bounds_text(bounds: tuple[float, float]) -> str:
    return f"[{_fmt(bounds[0])}, {_fmt(bounds[1])}]"


def effective_bounds(
    policy: Policy,
    domain: str,
    targets: list[EntityState],
    fields: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, tuple[float, float]]:
    rules = policy.domains[domain]
    result = {}
    for name in rules.fields:
        sources: list[tuple[float | None, float | None]] = []
        if name in rules.bounds:
            sources.append(rules.bounds[name])
        if fields and name in fields:
            sources.append(selector_bounds(fields[name]))
        if (domain, name) in ATTRIBUTE_BOUNDS:
            low_attr, high_attr = ATTRIBUTE_BOUNDS[(domain, name)]
            for target in targets:
                attrs = target.attributes
                sources.append((_num(attrs.get(low_attr)), _num(attrs.get(high_attr))))
        bounds = _intersect(sources)
        if bounds is not None:
            result[name] = bounds
    return result


def _check_value(
    domain: str,
    name: str,
    value: Any,
    bounds: tuple[float, float] | None,
    targets: list[EntityState],
) -> Check:
    check = f"value.{name}"
    if bounds is not None:
        number = _num(value)
        if number is None:
            return Check(check, False, f"{value!r} is not a number")
        if bounds[0] > bounds[1]:
            return Check(check, False, "the targets have no common allowed range")
        if not bounds[0] <= number <= bounds[1]:
            return Check(check, False, f"{_fmt(number)} is outside {_bounds_text(bounds)}")
        return Check(check, True, f"{_fmt(number)} is within {_bounds_text(bounds)}")
    if (domain, name) in ATTRIBUTE_OPTIONS:
        attr = ATTRIBUTE_OPTIONS[(domain, name)]
        for target in targets:
            options = target.attributes.get(attr)
            if isinstance(options, list) and value not in options:
                return Check(
                    check,
                    False,
                    f"{value!r} is not supported by {target.entity_id}; options: "
                    + ", ".join(map(str, options)),
                )
    if not isinstance(value, str | int | float | bool):
        return Check(check, False, "only plain values (string, number, boolean) are allowed")
    return Check(check, True, f"{value!r} accepted")


def evaluate(
    policy: Policy,
    request: ActionRequest,
    states: Mapping[str, EntityState],
    services: Mapping[str, Any] | None = None,
) -> Decision:
    """Check a requested action against the policy. `services` is HA's `get_services`
    payload; without it the schema checks (service exists, required fields) are skipped.
    """
    domain, _, service = request.service.partition(".")
    if not policy.allows(request.service):
        allowed = sorted(f"{d}.{s}" for d, rules in policy.domains.items() for s in rules.services)
        detail = f"{request.service} is not allowed by the action policy; allowed: " + (
            ", ".join(allowed) or "nothing"
        )
        return Decision(False, None, False, [Check("service_allowed", False, detail)], {})

    rules = policy.domains[domain]
    checks = [Check("service_allowed", True, f"{request.service} is allowed")]
    fields = None
    if services is not None:
        fields = schema_fields(services, domain, service)
        checks.append(
            Check("service_exists", True, "service is registered in Home Assistant")
            if fields is not None
            else Check("service_exists", False, f"{request.service} does not exist in HA")
        )

    ids = request.entity_ids
    limit = policy.defaults.max_entities
    checks.append(
        Check("entity_count", 1 <= len(ids) <= limit, f"{len(ids)} target(s), limit {limit}")
    )
    unknown = [e for e in ids if e not in states]
    checks.append(
        Check("entities_exist", not unknown, "missing: " + ", ".join(unknown))
        if unknown
        else Check("entities_exist", True, "all targets exist")
    )
    wrong_domain = [e for e in ids if e.partition(".")[0] != domain]
    checks.append(
        Check("entities_domain", False, f"not {domain} entities: " + ", ".join(wrong_domain))
        if wrong_domain
        else Check("entities_domain", True, f"all targets are {domain} entities")
    )
    denied = [e for e in ids if e in policy.deny.entities]
    checks.append(
        Check("entities_not_denied", False, "denied by policy: " + ", ".join(denied))
        if denied
        else Check("entities_not_denied", True, "no target is on the deny list")
    )
    targets = [states[e] for e in ids if e in states and e.partition(".")[0] == domain]
    offline = [t.entity_id for t in targets if t.state == "unavailable"]
    checks.append(
        Check("entities_available", False, "unavailable: " + ", ".join(offline))
        if offline
        else Check("entities_available", True, "all targets are available")
    )

    not_allowed = sorted(set(request.data) - set(rules.fields))
    if not_allowed:
        checks.append(
            Check(
                "fields_allowed",
                False,
                f"fields not allowed: {', '.join(not_allowed)}; allowed: "
                + (", ".join(rules.fields) or "none"),
            )
        )
    else:
        checks.append(Check("fields_allowed", True, "all fields are allowed"))
    if fields is not None:
        unknown_fields = sorted(set(request.data) - set(fields))
        missing = sorted(n for n, spec in fields.items() if spec.get("required"))
        missing = [n for n in missing if n not in request.data]
        if unknown_fields:
            checks.append(
                Check(
                    "fields_valid",
                    False,
                    f"{request.service} has no fields: " + ", ".join(unknown_fields),
                )
            )
        elif missing:
            checks.append(Check("fields_valid", False, "required fields: " + ", ".join(missing)))
        else:
            checks.append(Check("fields_valid", True, "fields match the service schema"))

    bounds = effective_bounds(policy, domain, targets, fields)
    for name, value in request.data.items():
        if name in rules.fields:
            checks.append(_check_value(domain, name, value, bounds.get(name), targets))

    risk = policy.risk_of(request.service)
    if len(ids) > policy.defaults.bulk_threshold:
        risk = raise_risk(risk)
    return Decision(
        allowed=all(c.ok for c in checks),
        risk=risk,
        needs_confirmation=risk != "low",
        checks=checks,
        effective_bounds=bounds,
    )
