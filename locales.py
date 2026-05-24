"""
DeepScan Local - Localization (15 languages)
Designer: Sedat Telli | sedattelli.com

Language priority:
  1. lang.txt  — written by the installer (user's install-time choice)
  2. Windows UI locale  — auto-detected via locale.getdefaultlocale()
  3. English fallback
"""

import locale
import os
from pathlib import Path

_APP_DATA = Path(os.environ.get("LOCALAPPDATA", "C:/Temp")) / "DeepScanLocal"
_LANG_FILE = _APP_DATA / "lang.txt"

# ---------------------------------------------------------------------------
# Translation table  (key → language code → translated string)
# ---------------------------------------------------------------------------
_T: dict[str, dict[str, str]] = {

    # ── English ─────────────────────────────────────────────────────────────
    "en": {
        "search_placeholder":  "Search files…  (Ctrl+Alt)",
        "no_results":          "No results found.",
        "results":             "result(s)",
        "score":               "Score",
        "open":                "Open",
        "disconnected":        "Disconnected",
        "indexing":            "Indexing…",
        "indexing_complete":   "Indexing complete.",
        "reindexing":          "Re-indexing…",
        "watcher_paused":      "File watcher paused.",
        "watcher_resumed":     "File watcher resumed.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt to search",
        "open_search":         "Open Search",
        "reindex":             "Re-index Now",
        "pause_watcher":       "Pause Watcher",
        "resume_watcher":      "Resume Watcher",
        "about":               "About",
        "exit":                "Exit",
        "about_title":         "About DeepScan Local",
        "about_body":          "DeepScan Local\nVersion 1.0\n\nDesigner: Sedat Telli\nsedattelli.com",
        "file_not_found":      "File no longer exists.",
    },

    # ── Türkçe ──────────────────────────────────────────────────────────────
    "tr": {
        "search_placeholder":  "Dosya ara…  (Ctrl+Alt)",
        "no_results":          "Sonuç bulunamadı.",
        "results":             "sonuç",
        "score":               "Puan",
        "open":                "Aç",
        "disconnected":        "Takılı Değil",
        "indexing":            "İndeksleniyor…",
        "indexing_complete":   "İndeksleme tamamlandı.",
        "reindexing":          "Yeniden indeksleniyor…",
        "watcher_paused":      "Dosya izleme duraklatıldı.",
        "watcher_resumed":     "Dosya izleme sürdürüldü.",
        "tray_tooltip":        "DeepScan Local — Arama için Ctrl+Alt",
        "open_search":         "Aramayı Aç",
        "reindex":             "Şimdi Yeniden İndeksle",
        "pause_watcher":       "İzlemeyi Duraklat",
        "resume_watcher":      "İzlemeyi Sürdür",
        "about":               "Hakkında",
        "exit":                "Çıkış",
        "about_title":         "DeepScan Local Hakkında",
        "about_body":          "DeepScan Local\nSürüm 1.0\n\nTasarımcı: Sedat Telli\nsedattelli.com",
        "file_not_found":      "Dosya artık mevcut değil.",
    },

    # ── Deutsch ─────────────────────────────────────────────────────────────
    "de": {
        "search_placeholder":  "Dateien suchen…  (Ctrl+Alt)",
        "no_results":          "Keine Ergebnisse gefunden.",
        "results":             "Ergebnis(se)",
        "score":               "Bewertung",
        "open":                "Öffnen",
        "disconnected":        "Getrennt",
        "indexing":            "Indizierung…",
        "indexing_complete":   "Indizierung abgeschlossen.",
        "reindexing":          "Neu indizieren…",
        "watcher_paused":      "Dateiüberwachung pausiert.",
        "watcher_resumed":     "Dateiüberwachung fortgesetzt.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt zum Suchen",
        "open_search":         "Suche öffnen",
        "reindex":             "Jetzt neu indizieren",
        "pause_watcher":       "Überwachung pausieren",
        "resume_watcher":      "Überwachung fortsetzen",
        "about":               "Über",
        "exit":                "Beenden",
        "about_title":         "Über DeepScan Local",
        "about_body":          "DeepScan Local\nVersion 1.0\n\nDesigner: Sedat Telli\nsedattelli.com",
        "file_not_found":      "Datei existiert nicht mehr.",
    },

    # ── Français ────────────────────────────────────────────────────────────
    "fr": {
        "search_placeholder":  "Rechercher des fichiers…  (Ctrl+Alt)",
        "no_results":          "Aucun résultat trouvé.",
        "results":             "résultat(s)",
        "score":               "Score",
        "open":                "Ouvrir",
        "disconnected":        "Déconnecté",
        "indexing":            "Indexation…",
        "indexing_complete":   "Indexation terminée.",
        "reindexing":          "Réindexation…",
        "watcher_paused":      "Surveillance suspendue.",
        "watcher_resumed":     "Surveillance reprise.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt pour chercher",
        "open_search":         "Ouvrir la recherche",
        "reindex":             "Réindexer maintenant",
        "pause_watcher":       "Suspendre la surveillance",
        "resume_watcher":      "Reprendre la surveillance",
        "about":               "À propos",
        "exit":                "Quitter",
        "about_title":         "À propos de DeepScan Local",
        "about_body":          "DeepScan Local\nVersion 1.0\n\nConcepteur: Sedat Telli\nsedattelli.com",
        "file_not_found":      "Le fichier n'existe plus.",
    },

    # ── Español ─────────────────────────────────────────────────────────────
    "es": {
        "search_placeholder":  "Buscar archivos…  (Ctrl+Alt)",
        "no_results":          "No se encontraron resultados.",
        "results":             "resultado(s)",
        "score":               "Puntuación",
        "open":                "Abrir",
        "disconnected":        "Desconectado",
        "indexing":            "Indexando…",
        "indexing_complete":   "Indexación completa.",
        "reindexing":          "Reindexando…",
        "watcher_paused":      "Supervisión de archivos pausada.",
        "watcher_resumed":     "Supervisión de archivos reanudada.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt para buscar",
        "open_search":         "Abrir búsqueda",
        "reindex":             "Reindexar ahora",
        "pause_watcher":       "Pausar supervisión",
        "resume_watcher":      "Reanudar supervisión",
        "about":               "Acerca de",
        "exit":                "Salir",
        "about_title":         "Acerca de DeepScan Local",
        "about_body":          "DeepScan Local\nVersión 1.0\n\nDiseñador: Sedat Telli\nsedattelli.com",
        "file_not_found":      "El archivo ya no existe.",
    },

    # ── العربية ──────────────────────────────────────────────────────────────
    "ar": {
        "search_placeholder":  "ابحث عن الملفات…  (Ctrl+Alt)",
        "no_results":          "لم يتم العثور على نتائج.",
        "results":             "نتيجة",
        "score":               "النتيجة",
        "open":                "فتح",
        "disconnected":        "غير متصل",
        "indexing":            "جارٍ الفهرسة…",
        "indexing_complete":   "اكتملت الفهرسة.",
        "reindexing":          "إعادة الفهرسة…",
        "watcher_paused":      "تم إيقاف مراقبة الملفات.",
        "watcher_resumed":     "تم استئناف مراقبة الملفات.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt للبحث",
        "open_search":         "فتح البحث",
        "reindex":             "إعادة الفهرسة الآن",
        "pause_watcher":       "إيقاف المراقبة",
        "resume_watcher":      "استئناف المراقبة",
        "about":               "حول",
        "exit":                "خروج",
        "about_title":         "حول DeepScan Local",
        "about_body":          "DeepScan Local\nالإصدار 1.0\n\nالمصمم: Sedat Telli\nsedattelli.com",
        "file_not_found":      "الملف لم يعد موجوداً.",
    },

    # ── Português ───────────────────────────────────────────────────────────
    "pt": {
        "search_placeholder":  "Pesquisar ficheiros…  (Ctrl+Alt)",
        "no_results":          "Nenhum resultado encontrado.",
        "results":             "resultado(s)",
        "score":               "Pontuação",
        "open":                "Abrir",
        "disconnected":        "Desligado",
        "indexing":            "A indexar…",
        "indexing_complete":   "Indexação concluída.",
        "reindexing":          "A reindexar…",
        "watcher_paused":      "Monitorização pausada.",
        "watcher_resumed":     "Monitorização retomada.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt para pesquisar",
        "open_search":         "Abrir pesquisa",
        "reindex":             "Reindexar agora",
        "pause_watcher":       "Pausar monitorização",
        "resume_watcher":      "Retomar monitorização",
        "about":               "Acerca de",
        "exit":                "Sair",
        "about_title":         "Acerca do DeepScan Local",
        "about_body":          "DeepScan Local\nVersão 1.0\n\nDesigner: Sedat Telli\nsedattelli.com",
        "file_not_found":      "O ficheiro já não existe.",
    },

    # ── Русский ─────────────────────────────────────────────────────────────
    "ru": {
        "search_placeholder":  "Поиск файлов…  (Ctrl+Alt)",
        "no_results":          "Результатов не найдено.",
        "results":             "результат(ов)",
        "score":               "Оценка",
        "open":                "Открыть",
        "disconnected":        "Отключено",
        "indexing":            "Индексирование…",
        "indexing_complete":   "Индексирование завершено.",
        "reindexing":          "Переиндексирование…",
        "watcher_paused":      "Мониторинг файлов приостановлен.",
        "watcher_resumed":     "Мониторинг файлов возобновлён.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt для поиска",
        "open_search":         "Открыть поиск",
        "reindex":             "Переиндексировать",
        "pause_watcher":       "Приостановить мониторинг",
        "resume_watcher":      "Возобновить мониторинг",
        "about":               "О программе",
        "exit":                "Выход",
        "about_title":         "О DeepScan Local",
        "about_body":          "DeepScan Local\nВерсия 1.0\n\nДизайнер: Sedat Telli\nsedattelli.com",
        "file_not_found":      "Файл больше не существует.",
    },

    # ── 日本語 ───────────────────────────────────────────────────────────────
    "ja": {
        "search_placeholder":  "ファイルを検索…  (Ctrl+Alt)",
        "no_results":          "結果が見つかりませんでした。",
        "results":             "件",
        "score":               "スコア",
        "open":                "開く",
        "disconnected":        "未接続",
        "indexing":            "インデックス作成中…",
        "indexing_complete":   "インデックス作成完了。",
        "reindexing":          "再インデックス作成中…",
        "watcher_paused":      "ファイル監視を一時停止しました。",
        "watcher_resumed":     "ファイル監視を再開しました。",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt で検索",
        "open_search":         "検索を開く",
        "reindex":             "今すぐ再インデックス",
        "pause_watcher":       "監視を一時停止",
        "resume_watcher":      "監視を再開",
        "about":               "バージョン情報",
        "exit":                "終了",
        "about_title":         "DeepScan Local について",
        "about_body":          "DeepScan Local\nバージョン 1.0\n\nデザイナー: Sedat Telli\nsedattelli.com",
        "file_not_found":      "ファイルが見つかりません。",
    },

    # ── 한국어 ───────────────────────────────────────────────────────────────
    "ko": {
        "search_placeholder":  "파일 검색…  (Ctrl+Alt)",
        "no_results":          "결과를 찾을 수 없습니다.",
        "results":             "개 결과",
        "score":               "점수",
        "open":                "열기",
        "disconnected":        "연결 해제됨",
        "indexing":            "인덱싱 중…",
        "indexing_complete":   "인덱싱 완료.",
        "reindexing":          "재인덱싱 중…",
        "watcher_paused":      "파일 감시 일시 중지됨.",
        "watcher_resumed":     "파일 감시 재개됨.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt로 검색",
        "open_search":         "검색 열기",
        "reindex":             "지금 재인덱싱",
        "pause_watcher":       "감시 일시 중지",
        "resume_watcher":      "감시 재개",
        "about":               "정보",
        "exit":                "종료",
        "about_title":         "DeepScan Local 정보",
        "about_body":          "DeepScan Local\n버전 1.0\n\n디자이너: Sedat Telli\nsedattelli.com",
        "file_not_found":      "파일이 더 이상 존재하지 않습니다.",
    },

    # ── 中文 (简体) ──────────────────────────────────────────────────────────
    "zh": {
        "search_placeholder":  "搜索文件…  (Ctrl+Alt)",
        "no_results":          "未找到结果。",
        "results":             "个结果",
        "score":               "评分",
        "open":                "打开",
        "disconnected":        "已断开",
        "indexing":            "正在建立索引…",
        "indexing_complete":   "索引建立完成。",
        "reindexing":          "正在重建索引…",
        "watcher_paused":      "文件监控已暂停。",
        "watcher_resumed":     "文件监控已恢复。",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt 搜索",
        "open_search":         "打开搜索",
        "reindex":             "立即重建索引",
        "pause_watcher":       "暂停监控",
        "resume_watcher":      "恢复监控",
        "about":               "关于",
        "exit":                "退出",
        "about_title":         "关于 DeepScan Local",
        "about_body":          "DeepScan Local\n版本 1.0\n\n设计师: Sedat Telli\nsedattelli.com",
        "file_not_found":      "文件已不存在。",
    },

    # ── हिन्दी ───────────────────────────────────────────────────────────────
    "hi": {
        "search_placeholder":  "फ़ाइलें खोजें…  (Ctrl+Alt)",
        "no_results":          "कोई परिणाम नहीं मिला।",
        "results":             "परिणाम",
        "score":               "स्कोर",
        "open":                "खोलें",
        "disconnected":        "डिस्कनेक्ट",
        "indexing":            "अनुक्रमणिका बना रहे हैं…",
        "indexing_complete":   "अनुक्रमणिका पूर्ण।",
        "reindexing":          "पुनः अनुक्रमणिका…",
        "watcher_paused":      "फ़ाइल मॉनिटरिंग रोकी गई।",
        "watcher_resumed":     "फ़ाइल मॉनिटरिंग फिर शुरू।",
        "tray_tooltip":        "DeepScan Local — खोज के लिए Ctrl+Alt",
        "open_search":         "खोज खोलें",
        "reindex":             "अभी पुनः अनुक्रमणिका",
        "pause_watcher":       "मॉनिटरिंग रोकें",
        "resume_watcher":      "मॉनिटरिंग फिर शुरू",
        "about":               "के बारे में",
        "exit":                "बाहर निकलें",
        "about_title":         "DeepScan Local के बारे में",
        "about_body":          "DeepScan Local\nसंस्करण 1.0\n\nडिज़ाइनर: Sedat Telli\nsedattelli.com",
        "file_not_found":      "फ़ाइल अब मौजूद नहीं है।",
    },

    # ── বাংলা ────────────────────────────────────────────────────────────────
    "bn": {
        "search_placeholder":  "ফাইল খুঁজুন…  (Ctrl+Alt)",
        "no_results":          "কোনো ফলাফল পাওয়া যায়নি।",
        "results":             "ফলাফল",
        "score":               "স্কোর",
        "open":                "খুলুন",
        "disconnected":        "সংযোগ বিচ্ছিন্ন",
        "indexing":            "সূচীকরণ চলছে…",
        "indexing_complete":   "সূচীকরণ সম্পন্ন।",
        "reindexing":          "পুনরায় সূচীকরণ…",
        "watcher_paused":      "ফাইল পর্যবেক্ষণ বিরতি দেওয়া হয়েছে।",
        "watcher_resumed":     "ফাইল পর্যবেক্ষণ পুনরায় শুরু হয়েছে।",
        "tray_tooltip":        "DeepScan Local — অনুসন্ধানের জন্য Ctrl+Alt",
        "open_search":         "অনুসন্ধান খুলুন",
        "reindex":             "এখনই পুনরায় সূচীকরণ",
        "pause_watcher":       "পর্যবেক্ষণ বিরতি",
        "resume_watcher":      "পর্যবেক্ষণ পুনরায় শুরু",
        "about":               "সম্পর্কে",
        "exit":                "প্রস্থান",
        "about_title":         "DeepScan Local সম্পর্কে",
        "about_body":          "DeepScan Local\nসংস্করণ 1.0\n\nডিজাইনার: Sedat Telli\nsedattelli.com",
        "file_not_found":      "ফাইলটি আর বিদ্যমান নেই।",
    },

    # ── اردو ─────────────────────────────────────────────────────────────────
    "ur": {
        "search_placeholder":  "فائلیں تلاش کریں…  (Ctrl+Alt)",
        "no_results":          "کوئی نتیجہ نہیں ملا۔",
        "results":             "نتائج",
        "score":               "اسکور",
        "open":                "کھولیں",
        "disconnected":        "غیر منسلک",
        "indexing":            "انڈیکسنگ…",
        "indexing_complete":   "انڈیکسنگ مکمل۔",
        "reindexing":          "دوبارہ انڈیکسنگ…",
        "watcher_paused":      "فائل مانیٹرنگ روک دی گئی۔",
        "watcher_resumed":     "فائل مانیٹرنگ دوبارہ شروع۔",
        "tray_tooltip":        "DeepScan Local — تلاش کے لیے Ctrl+Alt",
        "open_search":         "تلاش کھولیں",
        "reindex":             "ابھی دوبارہ انڈیکس کریں",
        "pause_watcher":       "مانیٹرنگ روکیں",
        "resume_watcher":      "مانیٹرنگ دوبارہ شروع",
        "about":               "کے بارے میں",
        "exit":                "باہر نکلیں",
        "about_title":         "DeepScan Local کے بارے میں",
        "about_body":          "DeepScan Local\nورژن 1.0\n\nڈیزائنر: Sedat Telli\nsedattelli.com",
        "file_not_found":      "فائل اب موجود نہیں ہے۔",
    },

    # ── Bahasa Indonesia ─────────────────────────────────────────────────────
    "id": {
        "search_placeholder":  "Cari file…  (Ctrl+Alt)",
        "no_results":          "Tidak ada hasil ditemukan.",
        "results":             "hasil",
        "score":               "Skor",
        "open":                "Buka",
        "disconnected":        "Terputus",
        "indexing":            "Mengindeks…",
        "indexing_complete":   "Pengindeksan selesai.",
        "reindexing":          "Mengindeks ulang…",
        "watcher_paused":      "Pemantauan file dijeda.",
        "watcher_resumed":     "Pemantauan file dilanjutkan.",
        "tray_tooltip":        "DeepScan Local — Ctrl+Alt untuk mencari",
        "open_search":         "Buka Pencarian",
        "reindex":             "Indeks Ulang Sekarang",
        "pause_watcher":       "Jeda Pemantauan",
        "resume_watcher":      "Lanjutkan Pemantauan",
        "about":               "Tentang",
        "exit":                "Keluar",
        "about_title":         "Tentang DeepScan Local",
        "about_body":          "DeepScan Local\nVersi 1.0\n\nDesainer: Sedat Telli\nsedattelli.com",
        "file_not_found":      "File tidak lagi ada.",
    },
}

# ---------------------------------------------------------------------------
# Language detection  (lang.txt → Windows locale → English fallback)
# ---------------------------------------------------------------------------

_LOCALE_MAP: dict[str, str] = {
    "tr": "tr", "de": "de", "fr": "fr", "es": "es", "ar": "ar",
    "pt": "pt", "ru": "ru", "ja": "ja", "ko": "ko", "zh": "zh",
    "hi": "hi", "bn": "bn", "ur": "ur", "id": "id",
}


def _detect_lang() -> str:
    # 1. lang.txt written by the installer
    try:
        if _LANG_FILE.exists():
            code = _LANG_FILE.read_text("utf-8").strip().lower()
            if code in _T:
                return code
    except Exception:
        pass

    # 2. Windows UI locale
    try:
        lang_code, _ = locale.getdefaultlocale()
        if lang_code:
            prefix = lang_code.lower()[:2]
            if prefix in _LOCALE_MAP:
                return _LOCALE_MAP[prefix]
    except Exception:
        pass

    return "en"


_LANG: str = _detect_lang()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_text(key: str) -> str:
    return _T.get(_LANG, _T["en"]).get(key, key)


def current_language() -> str:
    return _LANG
