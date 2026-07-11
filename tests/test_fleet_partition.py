"""Round-robin da frota (3 workers)."""

from app.bot.fleet_workers import partition_round_robin


def test_round_robin_1_based_mapping():
    # posições 1..13 → workers 1,2,3,1,2,3...
    vehicles = [{"placa": f"P{i:02d}"} for i in range(1, 14)]
    buckets = partition_round_robin(vehicles, 3)
    # Worker 1 (index 0): 1,4,7,10,13
    assert [v["placa"] for _, v in buckets[0]] == ["P01", "P04", "P07", "P10", "P13"]
    # Worker 2: 2,5,8,11
    assert [v["placa"] for _, v in buckets[1]] == ["P02", "P05", "P08", "P11"]
    # Worker 3: 3,6,9,12
    assert [v["placa"] for _, v in buckets[2]] == ["P03", "P06", "P09", "P12"]


def test_empty_and_small():
    assert partition_round_robin([], 3) == [[], [], []]
    b = partition_round_robin([{"placa": "AAA1B23"}], 3)
    assert len(b[0]) == 1
    assert b[1] == [] and b[2] == []
