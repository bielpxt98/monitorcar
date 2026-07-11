"""Round-robin da frota (3 workers)."""

from app.bot.fleet_workers import partition_round_robin


def test_round_robin_two_workers():
    # 2 Chromes: 1,3,5… e 2,4,6…
    vehicles = [{"placa": f"P{i:02d}"} for i in range(1, 11)]
    buckets = partition_round_robin(vehicles, 2)
    assert [v["placa"] for _, v in buckets[0]] == ["P01", "P03", "P05", "P07", "P09"]
    assert [v["placa"] for _, v in buckets[1]] == ["P02", "P04", "P06", "P08", "P10"]


def test_empty_and_small():
    assert partition_round_robin([], 2) == [[], []]
    b = partition_round_robin([{"placa": "AAA1B23"}], 2)
    assert len(b[0]) == 1
    assert b[1] == []
