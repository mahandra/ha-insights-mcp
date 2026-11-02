from ha_insights_mcp.ha.models import EntityState, Registries
from ha_insights_mcp.snapshot import HomeSnapshot, SnapshotProvider
from tests.conftest import REGISTRY_RESULTS, STATES


def make_states() -> list[EntityState]:
    return [EntityState.model_validate(s) for s in STATES]


def make_registries() -> Registries:
    return Registries(
        areas=REGISTRY_RESULTS["config/area_registry/list"],
        devices=REGISTRY_RESULTS["config/device_registry/list"],
        entities=REGISTRY_RESULTS["config/entity_registry/list"],
    )


class FakeHAClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_states(self) -> list[EntityState]:
        self.calls += 1
        return make_states()

    async def get_registries(self) -> Registries:
        return make_registries()


def test_area_resolution() -> None:
    snap = HomeSnapshot.build(make_states(), make_registries())

    # Inherited from device.
    assert snap.entities["light.kitchen_lights"].area.area_id == "kitchen"
    assert snap.entities["climate.hvac"].area.area_id == "bedroom"
    assert snap.entities["climate.hvac"].device.display_name == "Main HVAC"
    # Entity-level area, no device.
    assert snap.entities["light.bed_light"].area.area_id == "bedroom"
    # In registry without area, and not in registry at all.
    assert snap.entities["sensor.outside_temperature"].area is None
    assert snap.entities["sun.sun"].area is None
    assert snap.entities["sun.sun"].device is None


def test_find_area_by_id_or_name() -> None:
    snap = HomeSnapshot.build(make_states(), make_registries())
    assert snap.find_area("kitchen").name == "Kitchen"
    assert snap.find_area("  KITCHEN ").area_id == "kitchen"
    assert snap.find_area("Bedroom").area_id == "bedroom"
    assert snap.find_area("attic") is None


async def test_provider_caches_within_ttl() -> None:
    now = [100.0]
    client = FakeHAClient()
    provider = SnapshotProvider(client, ttl=10, clock=lambda: now[0])

    first = await provider.get()
    now[0] += 5
    assert await provider.get() is first
    assert client.calls == 1

    now[0] += 6
    assert await provider.get() is not first
    assert client.calls == 2


async def test_provider_ttl_zero_always_refreshes() -> None:
    client = FakeHAClient()
    provider = SnapshotProvider(client, ttl=0)
    await provider.get()
    await provider.get()
    assert client.calls == 2
