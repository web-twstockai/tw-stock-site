"""停止買賣／上市櫃日期的官方證據（2026-11-17 人工查證後固化），供 halt_fix 寫入 trading_halts 與 price_gaps。

證據等級（evidence_level）：
  A＝交易所／櫃買中心官方網站頁面、PDF 或 API，查證當下直接取得全文；
  B＝公開資訊觀測站重大訊息或交易所／櫃買公告全文，但經轉載網站（鉅亨網、MoneyDJ、券商資訊站）取得，非官方網站原頁。
起迄日期的定義：start＝停止買賣的第一個日曆日（halt_date）；end＝恢復買賣（或終止上市）日，**不含**（區間 [start, end)）；
end=None 表示查證當下仍在停止買賣（以 tpex_cmode／TWSE 停止買賣 API 快照持續確認）。
絕不由程式推定停牌：沒有列在此處、也沒有 trading_halts 涵蓋的缺口一律記為「資料不足」。
"""
from __future__ import annotations

STOP_EVIDENCE: list[dict] = [
    {"symbol": "1589", "market": "TWSE", "name": "永冠-KY", "start": "2026-04-07", "end": "2026-11-18", "level": "A",
     "reason": "未依限公告申報財報＋CB 違約，停止買賣；停買滿六個月，2026-11-18 終止上市（end＝終止上市日）",
     "source": "https://www.twse.com.tw/rwd/zh/violation/stop?response=json",
     "also": ["https://investoredu.twse.com.tw/FileSystem/FileUpload/dabe270e-6dc9-46c6-a54b-1d0428de22da.pdf"],
     "checked": "2026-11-17 16:57（API 回 115年04月07日起停止買賣；PDF 為證交所 2026-10-08 新聞稿）"},
    {"symbol": "1435", "market": "TWSE", "name": "中福", "start": "2026-04-29", "end": "2026-06-18", "level": "A",
     "reason": "逾期未重新申報 114 年度財報，併案停止買賣；2026-06-18 恢復買賣",
     "source": "https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69e6379f4019ecfcd2cc10244.pdf",
     "also": ["https://news.cnyes.com/news/id/6434141"],
     "checked": "2026-11-17 16:55（恢復日取自證交所 PDF；起日 2026-04-29 取自 MOPS 重訊轉載，等級 B）"},
    {"symbol": "3714", "market": "TWSE", "name": "富采", "start": "2026-11-05", "end": "2026-11-16", "level": "A",
     "reason": "現金減資換發股票，舊股停止買賣 11/05～11/13；新股 11/16 上市買賣（臺證上一字第1150021151號）",
     "source": "https://accessibility.twse.com.tw/zh/announcement/announcement/detail.html?id=5E941495CDC011F19A80005056BE3760",
     "also": [], "checked": "2026-11-17 16:50（搜尋結果取得公告全文）"},
    {"symbol": "4534", "market": "TPEx", "name": "慶騰", "start": "2026-11-05", "end": "2026-11-16", "level": "B",
     "reason": "減資彌補虧損換發新股，暫停櫃買 11/05～11/13；新股 11/16 開始櫃買",
     "source": "https://just.honsec.com.tw/z/zf/zfz/zfz_F75C620C-BDF0-46ED-8298-0D8E43BB80B1.djhtm",
     "also": [], "checked": "2026-11-17 16:50（櫃買中心公告全文，券商資訊站轉載）"},
    {"symbol": "4804", "market": "TPEx", "name": "大略-KY", "start": "2026-04-14", "end": None, "level": "A",
     "reason": "繼續經營能力重大不確定逾三年等，停止櫃買；2026-09-03 重訊：另因經營權異動／營業範圍重大變更仍停止買賣；至 2026-11-17 tpex_cmode 仍標停止交易",
     "source": "https://www.tpex.org.tw/openapi/v1/tpex_cmode",
     "also": ["https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=3b665845-8cb4-40e4-873b-ca77c2002625"],
     "checked": "2026-11-17 16:58（tpex_cmode 快照 1151117：SuspensionOfTrading=Ｙ；起日 2026-04-14 取自 MOPS 重訊轉載，等級 B）"},
    {"symbol": "1591", "market": "TPEx", "name": "駿吉-KY", "start": "2026-06-11", "end": "2026-07-22", "level": "A",
     "reason": "未依限重行公告申報 115 年第 1 季財報，停止櫃買；2026-07-22 恢復買賣",
     "source": "https://www.tpex.org.tw/storage/eb_data/11506/11500632304.html",
     "also": ["https://news.cnyes.com/news/id/6540699"],
     "checked": "2026-11-17 16:55（起日取自櫃買中心公告全文；恢復日 07-22 取自 MOPS 重訊轉載，等級 B）"},
    {"symbol": "3086", "market": "TPEx", "name": "華義*", "start": "2026-04-09", "end": "2026-04-20", "level": "B",
     "reason": "變更股票面額（10→1 元）換發股票，舊股停止櫃買 04/09～04/17；新股 04/20 上櫃買賣",
     "source": "https://tw.stock.yahoo.com/news/%E5%85%AC%E5%91%8A-%E8%8F%AF%E7%BE%A9%E8%82%A1%E7%A5%A8%E9%9D%A2%E9%A1%8D%E8%AE%8A%E6%9B%B4%E7%9B%B8%E9%97%9C%E4%BA%8B%E5%AE%9C-064742638.html",
     "also": [], "checked": "2026-11-17 16:53（MOPS 重訊載明新股 04-20 上櫃；起日 04-09 取自查證摘要，原文頁面未直接載明）"},
    {"symbol": "8937", "market": "TPEx", "name": "合騏*", "start": "2026-04-01", "end": "2026-04-13", "level": "B",
     "reason": "變更股票面額（10→2.5 元）全面換發，停止櫃買 04/01～04/10；04/13 恢復（證櫃監字第11500013831號）",
     "source": "https://www.capitalim.com.tw/newsite/stock-news/stock-news/9cbd07b5-9580-4f02-9a53-598d411e762a;page=1",
     "also": [], "checked": "2026-11-17 16:53（櫃買中心公告全文，投顧網站轉載）"},
    {"symbol": "4207", "market": "TPEx", "name": "環泰", "start": "2026-10-29", "end": "2026-11-09", "level": "B",
     "reason": "現金減資換發新股，暫停櫃買 10/29～11/06；新股 11/09 開始櫃買",
     "source": "https://just.honsec.com.tw/z/zf/zfz/zfz_1276FC6B-7255-4CD2-892B-FA97D64EBFFB.djhtm",
     "also": ["https://www.skis.com.tw/Doc/Report/insights/insights_20261106_085700.pdf"],
     "checked": "2026-11-17 16:53（櫃買中心公告全文，券商資訊站轉載）"},
    {"symbol": "5355", "market": "TPEx", "name": "佳總", "start": "2026-10-29", "end": "2026-11-09", "level": "B",
     "reason": "減資換股，暫停櫃買 10/29～11/06；新股 11/09 開始櫃買",
     "source": "https://www.skis.com.tw/Doc/Report/insights/insights_20261106_085700.pdf",
     "also": ["https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=7c88cbd6-42d5-45e4-82f9-11318b5a03c6"],
     "checked": "2026-11-17 16:53（近期現金減資個股表，資料來源：公開資訊觀測站）"},
    {"symbol": "8176", "market": "TPEx", "name": "智捷", "start": "2026-10-29", "end": "2026-11-09", "level": "B",
     "reason": "減資彌補虧損換發新股，停止櫃買 10/29～11/06；新股 11/09 開始櫃買",
     "source": "https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=2ce2ba60-4000-46a6-bd3b-c22619ebe866",
     "also": ["https://www.skis.com.tw/Doc/Report/insights/insights_20261106_085700.pdf"],
     "checked": "2026-11-17 16:53"},
]

# 尚未在該市場上市櫃（同代號）：start 之前「沒有行情」是正常的，不是停牌也不是資料缺漏
LISTING_EVIDENCE: list[dict] = [
    {"symbol": "3718", "market": "TPEx", "name": "中光電投控", "listed_from": "2026-09-03", "level": "B",
     "reason": "中光電（5371）股份轉換為中光電投控，新代號 3718 於 2026-09-03 上櫃（舊代號 5371 於 08-24 起停止櫃買、09-03 終止上櫃）；securities.listed_date（1999-01-20）是 ISIN 清單的舊值",
     "source": "https://www.tpex.org.tw/storage/eb_data/11508/11500050581.html",
     "also": ["https://news.cnyes.com/news/id/6550567"], "checked": "2026-11-17 16:50（櫃買中心公告證櫃監字第11500050581號）"},
    {"symbol": "1780", "market": "TPEx", "name": "立弘", "listed_from": "2026-06-17", "level": "B",
     "reason": "興櫃轉上櫃：2026-06-17 起上櫃買賣並終止興櫃（此前不在上櫃行情表）；securities.listed_date（2010-04-29）是興櫃／舊值",
     "source": "https://news.cnyes.com/news/id/6501504",
     "also": ["https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=357e8253-ab9a-4c0f-921c-1b1bf849238f"], "checked": "2026-11-17 16:50（MOPS 重訊轉載）"},
]
