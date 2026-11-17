"""重訊快照累積起始日（backfill_state material_snapshot/since）：只在第一次寫入、之後不被覆蓋；網站可據此顯示「資料不足（重訊自 MM/DD 起累積）」。"""
from pipeline import db as pdb
from pipeline.runstats import material_since, state_init


def test_since_written_once_and_never_overwritten():
    c = pdb.connect()
    pdb.init_db(c)
    assert material_since(c) is None                                # 尚未累積：網站顯示「資料不足（尚未開始累積）」
    assert state_init(c, "material_snapshot", "since", "info", "2026-11-17") is True
    assert state_init(c, "material_snapshot", "since", "info", "2026-11-20") is False   # 後續每日快照不改起始日
    c.commit()
    assert material_since(c) == "2026-11-17"
    r = c.execute("SELECT status, attempts FROM backfill_state WHERE job='material_snapshot' AND key='since'").fetchone()
    assert (r[0], r[1]) == ("info", 0)
