import sys
sys.path.insert(0, '.')

print("=== parse_query testi ===")
from main import parse_query
q, f = parse_query("rapor ext:pdf modified:today")
print(f"clean={q!r}  filters={f}")

q2, f2 = parse_query("fatura size:>1mb")
print(f"clean={q2!r}  filters={f2}")

print("\n=== indexer testi ===")
from indexer import get_index_stats, Indexer
stats = get_index_stats()
print(f"Toplam dosya : {stats['total_files']}")
print(f"DB boyutu    : {stats['db_size_bytes']/1024/1024:.1f} MB")
print(f"Suruculer    : {stats['by_drive']}")

idx = Indexer()
r1 = idx.search_names_only("rapor", limit=5)
print(f"\nsearch_names_only('rapor') -> {len(r1)} sonuc")
for r in r1[:3]:
    print(f"  {r['path']}")

r2 = idx.search("rapor", limit=10, filters={"extensions": [".pdf"]})
print(f"\nsearch('rapor', ext:pdf) -> {len(r2)} sonuc")
for r in r2[:3]:
    print(f"  {r['path']}")

print("\nTum testler tamam!")
