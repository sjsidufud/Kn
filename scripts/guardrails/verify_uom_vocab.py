#!/usr/bin/env python3
"""INV-UOM-02 — kosakata satuan dokumen WAJIB ada di master `uoms` (kode/nama/alias).

KELAS BUG YANG DICEGAH (D1, terukur 2026-08-18 & diverifikasi ulang 2026-08-19)
==============================================================================
Dokumen menyimpan satuan sebagai **kata**: `yard` · `kg` · `meter`. Master `uoms`
menyimpan **kode**: `MTR` · `YRD` · `RLL` · `PCS`. Tak satu pun nilai satuan yang
tersimpan di 16 tempat dokumen cocok dengan satu baris master pun. Akibat nyata:
  * pemilik menambah baris `KG` di master → **tidak ada yang berubah di layar**;
  * pemilih satuan di layar tidak bisa dibangun dari master (harus diketik ulang di
    kode) → daftar satuan di layar & di master boleh berbeda tanpa ada yang tahu;
  * satuan salah ketik (`hasta`, `yrd2`) tersimpan tanpa pernah ditolak, dan
    `uom_service` tidak akan bisa menyelesaikan faktornya → konversi 400 di kemudian
    hari, jauh dari tempat kesalahan dibuat.

ATURAN (DATA — butuh Mongo)
===========================
  A. Setiap nilai satuan yang tersimpan di `uom_service.UNIT_DOC_FIELDS` (16 koleksi ·
     19 field, termasuk `products.base_unit`) WAJIB cocok — huruf besar/kecil diabaikan —
     dengan `code`, `name`, atau salah satu `aliases` sebuah baris `uoms` **aktif**.
  B. Satu kata satuan hanya boleh menunjuk SATU baris master (alias tidak boleh kembar);
     kalau kembar, faktor & pembulatan untuk kata itu jadi tak tentu.
  C. Setiap baris master aktif ber-`base_type="length"`/`"weight"` WAJIB punya
     `factor_to_base > 0` (satuan tanpa faktor = konversi mustahil, senyap).

Jalankan:
    python scripts/guardrails/verify_uom_vocab.py
    python scripts/guardrails/verify_uom_vocab.py --self-test
"""
import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from _common import BACKEND, Guard, B, C, G, R, X  # noqa: E402

sys.path.insert(0, str(BACKEND))


def _db():
    if not os.environ.get("MONGO_URL"):
        from dotenv import load_dotenv
        load_dotenv(BACKEND / ".env")
    from pymongo import MongoClient
    cli = MongoClient(os.environ["MONGO_URL"].strip('"'), serverSelectionTimeoutMS=2500)
    db = cli[os.environ.get("DB_NAME", "test_database").strip('"')]
    db.command("ping")
    return db


def _unit_fields() -> Dict[str, List[str]]:
    """Peta koleksi→field satuan diambil dari SSOT backend (bukan salinan di gate)."""
    from services.uom_service import UNIT_DOC_FIELDS
    return UNIT_DOC_FIELDS


def vocab_of(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """{kata (huruf kecil) → kode master} untuk baris AKTIF."""
    out: Dict[str, str] = {}
    for r in rows:
        if (r.get("status") or "active") != "active":
            continue
        for k in [r.get("code"), r.get("name")] + list(r.get("aliases") or []):
            k = str(k or "").strip().lower()
            if k:
                out.setdefault(k, str(r.get("code") or ""))
    return out


def alias_clashes(rows: List[Dict[str, Any]]) -> List[str]:
    """Aturan B — satu kata dipakai dua baris master aktif."""
    pemilik: Dict[str, List[str]] = {}
    for r in rows:
        if (r.get("status") or "active") != "active":
            continue
        kode = str(r.get("code") or "?")
        for k in [r.get("code"), r.get("name")] + list(r.get("aliases") or []):
            k = str(k or "").strip().lower()
            if k:
                pemilik.setdefault(k, [])
                if kode not in pemilik[k]:
                    pemilik[k].append(kode)
    return [f"kata satuan `{k}` dipakai {len(v)} baris master ({', '.join(v)}) — "
            f"faktor & pembulatan untuk kata itu jadi tak tentu"
            for k, v in sorted(pemilik.items()) if len(v) > 1]


def check_data(db, rows: List[Dict[str, Any]]) -> Tuple[List[str], int, int]:
    """→ (pelanggaran, jumlah nilai satuan diperiksa, jumlah kata unik)."""
    viol: List[str] = []
    vocab = vocab_of(rows)
    diperiksa = 0
    kata: Dict[str, Dict[str, int]] = {}      # kata → {koleksi: jumlah}
    for col, fields in _unit_fields().items():
        for f in fields:
            for v in db[col].distinct(f):
                if v in (None, ""):
                    continue
                w = str(v).strip().lower()
                diperiksa += 1
                n = db[col].count_documents({f: v})
                kata.setdefault(w, {})
                kata[w][f"{col}.{f}"] = kata[w].get(f"{col}.{f}", 0) + n
    for w, tempat in sorted(kata.items()):
        if w in vocab:
            continue
        total = sum(tempat.values())
        rinci = ", ".join(f"{k} ×{v}" for k, v in sorted(tempat.items())[:4])
        viol.append(f"satuan `{w}` dipakai {total} dokumen ({rinci}) tetapi TIDAK ADA di "
                    f"master `uoms` (kode/nama/alias aktif) — pemilih satuan di layar tak "
                    f"menawarkannya & faktornya tak bisa diselesaikan. Tambahkan sebagai "
                    f"alias baris yang tepat lewat Master Data → UOM.")
    viol.extend(alias_clashes(rows))
    for r in rows:                                            # aturan C
        if (r.get("status") or "active") != "active":
            continue
        if str(r.get("base_type") or "").lower() in ("length", "weight"):
            try:
                f = float(r.get("factor_to_base") or 0)
            except (TypeError, ValueError):
                f = 0.0
            if f <= 0:
                viol.append(f"satuan {r.get('code')} ({r.get('base_type')}) tanpa "
                            f"`factor_to_base` > 0 — konversi ke satuan dasar mustahil")
    return viol, diperiksa, len(kata)


def main() -> int:
    g = Guard("INV-UOM-02", "kosakata satuan dokumen ⊆ master `uoms` (kode/nama/alias)")
    try:
        db = _db()
    except Exception as exc:  # noqa: BLE001
        print(f"  Mongo tak terjangkau ({exc}) — gate dilewati.")
        return 0
    rows = list(db.uoms.find({}, {"_id": 0}))
    viol, diperiksa, unik = check_data(db, rows)
    aktif = sum(1 for r in rows if (r.get("status") or "active") == "active")
    g.bump(diperiksa + aktif)
    print(f"  master satuan aktif: {aktif} · nilai satuan tersimpan diperiksa: {diperiksa} "
          f"({unik} kata unik) · koleksi dipantau: {len(_unit_fields())}")
    for v in viol:
        g.add(v)
    return g.finish()


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST — wajib MEMERAH pada pelanggaran buatan (data & master), dan wajib
# TIDAK menuduh keadaan yang sah. Data uji ditulis lalu DIHAPUS (nol residu).
# ─────────────────────────────────────────────────────────────────────────────
def self_test() -> int:
    kasus: List[Tuple[str, bool]] = []

    def cek(nama: str, benar: bool):
        kasus.append((nama, benar))

    ROWS_SAH = [
        {"code": "YRD", "name": "Yard", "base_type": "length", "factor_to_base": 0.9144,
         "aliases": ["yard", "yd"], "status": "active"},
        {"code": "KG", "name": "Kilogram", "base_type": "weight", "factor_to_base": 1.0,
         "aliases": ["kg"], "status": "active"},
    ]
    cek("alias kembar antar baris master → MERAH",
        len(alias_clashes(ROWS_SAH + [{"code": "YARD2", "name": "Yard lain",
                                       "aliases": ["yard"], "status": "active"}])) == 1)
    cek("master sah (tanpa alias kembar) → hijau", alias_clashes(ROWS_SAH) == [])
    cek("baris NONAKTIF tidak dihitung bentrok",
        alias_clashes(ROWS_SAH + [{"code": "OLD", "aliases": ["yard"],
                                   "status": "inactive"}]) == [])
    v = vocab_of(ROWS_SAH)
    cek("kata dokumen `yard` dikenali lewat alias", v.get("yard") == "YRD")
    cek("kode master `YRD` juga dikenali (huruf kecil)", v.get("yrd") == "YRD")
    cek("kata asing `hasta` TIDAK dikenali", "hasta" not in v)

    # ── Lapis DATA: bukti-merah pada basis data SUNGGUHAN (lalu dibersihkan) ──
    try:
        db = _db()
    except Exception as exc:  # noqa: BLE001
        print(f"{R}  Mongo tak terjangkau ({exc}) — lapis DATA self-test dilewati.{X}")
        db = None
    if db is not None:
        rows = list(db.uoms.find({}, {"_id": 0}))
        viol0, _, _ = check_data(db, rows)
        cek(f"kode nyata saat ini HIJAU ({len(viol0)} pelanggaran)", not viol0)
        probe = {"id": "wt_uomvocab_probe", "unit": "hasta", "status": "draft",
                 "type": "inbound", "entity_id": "ent_ksc", "_probe": True}
        db.wms_tasks.insert_one(dict(probe))
        try:
            viol1, _, _ = check_data(db, rows)
            cek("satuan `hasta` disuntik ke wms_tasks → MERAH",
                any("hasta" in x for x in viol1))
            cek("pesannya menyebut JUMLAH dokumen pemakainya",
                any("hasta" in x and "1 dokumen" in x for x in viol1))
        finally:
            db.wms_tasks.delete_one({"id": "wt_uomvocab_probe"})
        viol2, _, _ = check_data(db, rows)
        cek("hijau lagi sesudah data uji dihapus (nol residu)", not viol2)
        cek("tidak ada sisa dokumen uji",
            db.wms_tasks.count_documents({"id": "wt_uomvocab_probe"}) == 0)
        # Aturan C — satuan panjang tanpa faktor
        viol3, _, _ = check_data(db, rows + [{"code": "HASTAX", "name": "Hasta",
                                              "base_type": "length", "factor_to_base": 0,
                                              "aliases": [], "status": "active"}])
        cek("satuan panjang tanpa `factor_to_base` → MERAH",
            any("HASTAX" in x for x in viol3))

    gagal = sum(0 if ok else 1 for _n, ok in kasus)
    print(f"{C}{B}== SELF-TEST INV-UOM-02 (kosakata satuan) =={X}")
    for nama, ok in kasus:
        print(f"  [{G + 'PASS' + X if ok else R + 'FAIL' + X}] {nama}")
    print(f"{G}  HIJAU — penjaga menangkap satuan asing di data NYATA, alias kembar, dan "
          f"satuan tanpa faktor; tanpa menuduh baris nonaktif maupun kata yang sah.{X}"
          if not gagal else f"{R}{B}  SELF-TEST MERAH ({gagal} kasus).{X}")
    return gagal


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(1 if self_test() else 0)
    raise SystemExit(main())
