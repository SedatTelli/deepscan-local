<div align="center">

# 🔍 DeepScan Local

**Windows için Anlık Masaüstü Dosya Arama Uygulaması**

Dosyalarınızı içerik, ad, uzantı ve tarih gibi filtrelerle anında bulun.  
Görev çubuğuna sabitlenebilen, sistem tepsisinde çalışan hafif bir arama motorudur.

[![İndir](https://img.shields.io/badge/⬇_İndir-DeepScan_Local_Setup-blue?style=for-the-badge)](https://github.com/SedatTelli/deepscan-local/releases/latest)
[![Platform](https://img.shields.io/badge/Platform-Windows_10%2F11-0078D4?style=for-the-badge&logo=windows)](https://github.com/SedatTelli/deepscan-local/releases/latest)
[![Lisans](https://img.shields.io/badge/Lisans-MIT-green?style=for-the-badge)](LICENSE)

---

### 📺 Kurulum ve Kullanım Videosu

[![DeepScan Local Kurulum Videosu](https://img.youtube.com/vi/uE4GP5j_XK0/maxresdefault.jpg)](https://youtu.be/uE4GP5j_XK0?si=gf921-vF3bZv3ZyQ)

▶ **[YouTube'da İzle — Kurulum & Kullanım Rehberi](https://youtu.be/uE4GP5j_XK0?si=gf921-vF3bZv3ZyQ)**

</div>

---

## ⬇ İndirme ve Kurulum

### Adım 1 — Kurulum Dosyasını İndirin

Aşağıdaki bağlantıya tıklayarak son sürümü indirin:

**➡ [DeepScanLocal_Setup.exe — En Son Sürüm](https://github.com/SedatTelli/deepscan-local/releases/latest)**

### Adım 2 — Kurulumu Çalıştırın

1. İndirilen `DeepScanLocal_Setup.exe` dosyasına çift tıklayın.
2. **SmartScreen uyarısı çıkarsa:** "Daha fazla bilgi" → "Yine de çalıştır" deyin.  
   *(Uyarı, sertifika olmaksızın dağıtılan tüm küçük uygulamalarda görünür — normaldir.)*
3. Kurulum sihirbazını takip edin ve "Kur" düğmesine tıklayın.
4. Program kurulup otomatik olarak başlar.

### Adım 3 — Görev Çubuğuna Sabitleyin ⭐ (İlk Yapmanız Gereken)

> Program başladıktan sonra **görev çubuğundaki DeepScan simgesine sağ tıklayın**  
> ve **"Görev çubuğuna sabitle"** seçeneğini seçin.  
> Bu sayede bilgisayarı her açtığınızda program otomatik başlar ve  
> her an tek tıkla erişilebilir olur.

---

## 🚀 Kullanım

### Aramayı Açma

| Yöntem | Açıklama |
|---|---|
| **Ctrl + Alt** | Varsayılan kısayol tuşu |
| Görev çubuğundaki simgeye tıklama | Arama penceresini açar/kapatır |
| Sistem tepsisi simgesine tıklama | "Aramayı Aç" seçeneği |

### Temel Arama

Arama kutusuna yazmaya başlayın — sonuçlar anında gelir.  
**İlk aşama** dosya adlarını, **ikinci aşama** içerik eşleşmelerini getirir.

### Filtreler ve Özel Sözdizimi

| Sözdizimi | Açıklama | Örnek |
|---|---|---|
| `ext:uzantı` | Uzantıya göre filtrele | `ext:pdf` · `ext:pdf,docx` |
| `size:>10mb` | Dosya boyutuna göre | `size:>5mb` · `size:<100kb` |
| `modified:today` | Değiştirilme tarihine göre | `modified:week` · `modified:month` |
| `before:YYYY-AA-GG` | Belirli tarihten önce | `before:2024-01-01` |
| `after:YYYY-AA-GG` | Belirli tarihten sonra | `after:2023-06-01` |
| `regex:desen` | Dosya adında regex eşleşmesi | `regex:rapor_\d{4}` |
| `AND` / `OR` / `NOT` | Boolean operatörler | `rapor AND yıllık` · `pdf NOT taslak` |

### Sonuçlarda Gezinme

| Tuş / Eylem | İşlev |
|---|---|
| **↑ ↓** | Sonuçlar arasında gezin |
| **Enter** | Seçili dosyayı aç |
| **Sağ tık** | Bağlam menüsü (Klasörde Göster, Kopyala, Terminal vb.) |
| **Tümünü Göster →** | 15'ten fazla sonuç varsa tüm listeyi aç |
| **Escape** | Arama penceresini kapat |

---

## ✨ Özellikler

- **Anlık arama** — FTS5 tam metin indeksleme (SQLite)
- **İçerik okuma** — PDF, Word, Excel, PowerPoint, metin dosyaları
- **OCR desteği** — Taranmış görsel/PDF dosyalarında metin tanıma *(Windows 10/11 yerleşik OCR)*
- **Klasör arama** — Dizin adları da arama kapsamında
- **Yinelenen dosya tespiti** — MD5 karma ile aynı dosyaları bulup silebilirsiniz
- **Explorer sağ tık menüsü** — Dosyalara sağ tıklayıp "DeepScan Local ile Ara"
- **Boolean operatörler** — AND, OR, NOT sözdizimi
- **Arama geçmişi** — Son aramalar otomatik kaydedilir
- **Önizleme penceresi** — Dosyayı açmadan içeriğini görün
- **15 dil desteği** — Türkçe, İngilizce, Almanca, Fransızca ve daha fazlası
- **Çoklu monitör** — Her DPI ve ölçek oranında doğru konumlanır
- **Acrylic cam efekti** — Modern Windows görünümü
- **Gerçek zamanlı izleme** — Yeni dosyalar anında indekse eklenir
- **Bağlantısız sürücü desteği** — USB takılı değilken de arama sonuçlarında gösterir

---

## ⚙ Ayarlar

Sistem tepsisi simgesine sağ tıklayıp **Ayarlar**'a girin veya arama penceresindeki dişli simgesine tıklayın.

| Sekme | İçerik |
|---|---|
| **Klasörler** | Taranacak/atlanacak klasörler, USB ve ağ sürücüleri |
| **Uzantılar** | Hangi dosya türlerinin indeksleneceği |
| **Kısayol** | Ctrl+Alt kombinasyonunu özelleştirin |
| **İstatistikler** | İndeks boyutu, son tarama zamanı |
| **Gelişmiş** | Explorer sağ tık menüsü, yinelenen dosya tespiti |

---

## 🖥 Sistem Gereksinimleri

| Gereksinim | Minimum |
|---|---|
| İşletim Sistemi | Windows 10 (1809+) veya Windows 11 |
| RAM | 150 MB |
| Disk | 50 MB (kurulum) + indeks için ek alan |
| .NET / Runtime | Gerekmez — bağımsız .exe |

---

## 🛠 Kaynaktan Derleme

```bash
# Bağımlılıkları yükle
pip install -r requirements.txt

# Geliştirme modunda çalıştır
python main.py

# .exe olarak derle
build_exe.bat
```

**Gereksinimler:** Python 3.10+, PyInstaller 6.3+

---

## 🌐 Desteklenen Diller

Türkçe · English · Deutsch · Français · Español · العربية · Português · Русский · 日本語 · 한국어 · 中文 · हिन्दी · বাংলা · اردو · Bahasa Indonesia

---

## 👤 Geliştirici

**Sedat Telli**  
[sedattelli.com](https://sedattelli.com)

---

<div align="center">

MIT Lisansı © 2026 Sedat Telli

</div>
