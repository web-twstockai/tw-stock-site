"""研究用（非正式程式）：候選篩選模式定義。每個模式回傳布林 Series（index 同特徵表 v）。
所有模式另外都要求：當日 analysis screen() 可評估（PASS／FAIL），在 e_run 統一套用。
warm＝需要的有效交易日數（統計窗口起點用）。參數事先定稿，未依結果調整（§11.2.3）。"""
import numpy as np

TV1 = 1e8   # 成交金額 1 億元（與 M1／M2 相同）


def modes(v):
    C, O, H, L, V = v["adj_close_twd"], v["adj_open_twd"], v["adj_high_twd"], v["adj_low_twd"], v["adj_volume_shares"]
    tv = v["amount_twd"] >= TV1
    up_trend = (C > v["ma20"]) & (v["ma20"] > v["ma60"])
    M = {}

    def add(mid, name, warm, cond, desc, src):
        M[mid] = dict(name=name, warm=warm, cond=cond.fillna(False).astype(bool), desc=desc, src=src)

    # ===== 現有 M1～M8 =====
    add("M1", "趨勢＋突破＋量能", 61, v["scr_status"] == "PASS",
        "analysis screen() 預設四條件 PASS：收盤>MA20>MA60、收盤>前20日最高、量≥前20日均量1.5倍、成交金額≥1億", "existing")
    add("M2", "均線多頭排列", 60, (C > v["ma5"]) & (v["ma5"] > v["ma20"]) & (v["ma20"] > v["ma60"]) & tv,
        "收盤>MA5>MA20>MA60、成交金額≥1億", "existing")
    add("M3", "月營收創12個月新高（事件版）", 60, v["rv_event_day"] & (v["rv_rev_high"] == True),
        "營收法定期限（次月10日）後第一個交易日，最新月營收嚴格大於前12個月最高（rev_12m_high-v1）", "existing")
    add("M4", "營收連續3個月年增（事件版）", 60, v["rv_event_day"] & (v["rv_yoy3_pos"] == True),
        "營收期限日，最近3個月年增率都>0", "existing")
    add("M5", "單季EPS年增（事件版）", 60, v["fn_event_day"] & (v["fn_eps_yoy_pos"] == True),
        "財報期限日，單季EPS>0且>去年同季", "existing")
    add("M6s", "創120日新高（M6的縮短版）", 121, (C > v["hh120_prior"]) & tv,
        "收盤>前120個有效交易日最高價（不含當日）、成交金額≥1億；M6原定250日，現有行情不足", "existing-adapted")
    add("M7", "營收創高＋趨勢（事件版）", 60, v["rv_event_day"] & (v["rv_rev_high"] == True) & up_trend,
        "M3 事件日且收盤>MA20>MA60", "existing")
    add("M8", "EPS成長＋營收連增＋趨勢（事件版）", 60,
        v["fn_event_day"] & (v["fn_eps_yoy_pos"] == True) & (v["rv_yoy3_pos"] == True) & up_trend,
        "財報期限日 M5 為真、最新月營收 M4 狀態為真、收盤>MA20>MA60", "existing")

    # ===== 新候選：只用行情 =====
    add("C01", "趨勢樣板（Minervini 縮短版）", 121,
        (C > v["ma20"]) & (v["ma20"] > v["ma60"]) & (v["ma60"] > v["ma120"]) & (v["ma120"] > v["ma120_prev20"])
        & (C >= 1.3 * v["ll120_incl"]) & (C >= 0.75 * v["hh120_incl"]) & (v["rs60_pct"] >= 0.70) & tv,
        "收盤>MA20>MA60>MA120、MA120高於20日前、收盤≥120日最低×1.3、收盤≥120日最高×0.75、60日漲幅排名前30%、成交金額≥1億（原版用50/150/200日與52週，縮短）", "minervini")
    add("C02", "Darvas 箱型突破", 61,
        v["dv_top"].notna() & (v["dv_top"] / v["dv_bot"] - 1 <= 0.15) & (C > v["dv_top"]) & (V >= 1.5 * v["avgv20_prior"]) & tv,
        "前20日最高價後已3日未再創高、其後低點也3日未破（箱頂／箱底），箱高≤15%，收盤突破箱頂、量≥前20日均量1.5倍、成交金額≥1億", "darvas")
    add("C03", "波動收縮突破（VCP 簡化）", 61,
        (v["seg1_range"] > v["seg2_range"]) & (v["seg2_range"] > v["seg3_range"]) & (v["seg3_range"] <= 0.12)
        & (v["avgv10_prior"] < v["avgv50_prior"]) & (C > v["hh20_prior"]) & (V >= 1.5 * v["avgv50_prior"]) & (C > v["ma60"]) & tv,
        "前60日分三段（各20日）高低幅度逐段縮小、最後一段≤12%、前10日均量<前50日均量（量縮），今日收盤突破前20日最高、量≥前50日均量1.5倍、收盤>MA60、成交金額≥1億", "vcp")
    add("C04", "布林壓縮後突破", 81,
        (v["bbw_prev"] <= v["bbw_prev_min60"] + 0.2 * (v["bbw_prev_max60"] - v["bbw_prev_min60"])) & (C > v["bb_up"]) & tv,
        "前一日布林(20,2)頻寬位於前60日範圍最低20%內，今日收盤>上軌、成交金額≥1億", "bollinger")
    add("C05", "接近120日高點", 120, (C >= 0.95 * v["hh120_incl"]) & (C > v["ma60"]) & tv,
        "收盤≥近120日最高價×0.95、收盤>MA60、成交金額≥1億（52週高點的縮短版）", "52wk")
    add("C06", "月線上穿季線（黃金交叉）", 61, (v["ma20"] > v["ma60"]) & (v["ma20_prev"] <= v["ma60_prev"]) & tv,
        "MA20 今日由下往上穿越 MA60、成交金額≥1億", "golden-cross")
    add("C07", "5日線上穿月線＋站上季線", 61,
        (v["ma5"] > v["ma20"]) & (v["ma5_prev"] <= v["ma20_prev"]) & (C > v["ma60"]) & tv,
        "MA5 今日上穿 MA20、收盤>MA60、成交金額≥1億", "ma-cross")
    add("C08", "KD 低檔黃金交叉", 60,
        (v["k9_prev"] <= v["d9_prev"]) & (v["k9"] > v["d9"]) & (v["k9_prev"] < 20) & tv,
        "KD(9,3,3) K 由下往上穿越 D、前一日 K<20、成交金額≥1億", "kd")
    add("C09", "RSI(2) 多頭回檔", 60, (C > v["ma60"]) & (v["rsi2"] <= 10) & tv,
        "收盤>MA60（原版200日，縮短）、RSI(2)≤10、成交金額≥1億", "rsi2")
    add("C10", "RSI(14) 上穿 50", 60, (v["rsi14_prev"] <= 50) & (v["rsi14"] > 50) & (C > v["ma20"]) & tv,
        "RSI(14) 由≤50 升到>50、收盤>MA20、成交金額≥1億", "rsi")
    add("C11", "MACD 零軸上黃金交叉", 60,
        (v["dif_prev"] <= v["dem_prev"]) & (v["dif"] > v["dem"]) & (v["dif"] > 0) & tv,
        "MACD(12,26,9) DIF 上穿訊號線且 DIF>0、成交金額≥1億", "macd")
    add("C12", "跳空上漲帶量", 61,
        (O > v["h_prev"]) & (C >= O) & (V >= 1.5 * v["avgv20_prior"]) & tv,
        "今日開盤>前一日最高（向上跳空）、收盤≥開盤（紅K）、量≥前20日均量1.5倍、成交金額≥1億", "gap")
    add("C13", "多頭回檔月線", 66,
        (v["ma20"] > v["ma60"]) & (v["ma20"] > v["ma20_prev5"]) & (L <= v["ma20"] * 1.01) & (C > v["ma20"]) & (C > O)
        & (V < v["avgv20_prior"]) & tv,
        "MA20>MA60 且 MA20 高於5日前，今日最低價觸及 MA20（±1%）後收盤站回 MA20 之上、收紅K、量<前20日均量（量縮回檔）、成交金額≥1億", "pullback")
    add("C14", "連三紅量增", 60, v["up3"] & v["vinc2"] & tv,
        "連續3日收紅K且收盤逐日走高、成交量連2日增加、成交金額≥1億", "3up")
    add("C15", "低波動多頭", 120, (v["vol60_pct"] <= 0.2) & up_trend & tv,
        "60日報酬標準差位於當日可評估股票最低20%、收盤>MA20>MA60、成交金額≥1億", "lowvol")
    add("C16", "相對強勢前10%", 61, (v["rs60_pct"] >= 0.90) & (C > v["ma20"]) & tv,
        "60日漲幅在當日可評估股票前10%、收盤>MA20、成交金額≥1億", "rs")
    add("C17", "均線糾結突破", 61,
        (v["ma_spread_prev_max5"] < 0.02) & (C > v["ma_max3"]) & (C > v["c_prev"]) & (V >= 1.25 * v["avgv20_prior"]) & tv,
        "前5日 MA5/10/20 最大最小差都<2%（糾結），今日收盤站上三線且上漲、量≥前20日均量1.25倍、成交金額≥1億", "ma-entangle")
    add("C18", "量縮整理突破", 61,
        (v["range20_prior"] <= 0.10) & (v["avgv5_prior"] <= 0.8 * v["avgv20_prior"]) & (C > v["hh20_prior"])
        & (V >= 2.0 * v["avgv20_prior"]) & tv,
        "前20日高低差≤10%、前5日均量≤前20日均量0.8倍（量縮），今日收盤突破前20日最高、量≥前20日均量2倍、成交金額≥1億", "squeeze-vol")
    add("C19", "口袋支點（Pocket Pivot）", 61,
        (v["ret1"] > 0) & (V > v["max_downv10_prior"]) & (C > v["ma10"]) & (C > v["ma60"]) & tv,
        "今日上漲、量大於前10日任何下跌日的量、收盤>MA10 且 >MA60、成交金額≥1億", "pocket-pivot")
    add("C20", "NR7 後突破", 61,
        (v["rng_prev"] < v["rng_prev6_min"]) & (C > v["h_prev"]) & (C > v["ma20"]) & tv,
        "前一日振幅小於再前6日每一日（NR7），今日收盤>前一日最高、收盤>MA20、成交金額≥1億", "nr7")
    add("C21", "20日通道突破（海龜）", 61, (C > v["hh20_prior"]) & tv,
        "收盤>前20個有效交易日最高價、成交金額≥1億（不加量能與均線條件）", "donchian")
    add("C22", "OBV 領先創高", 61,
        (v["obv"] > v["obv_hh60_prior"]) & (C < v["hh60_prior"]) & (C > v["ma20"]) & tv,
        "OBV 創前60日新高但收盤尚未超過前60日最高價、收盤>MA20、成交金額≥1億", "obv")
    add("C23", "ADX 趨勢轉強", 61,
        (v["adx"] > 25) & (v["pdi_prev"] <= v["mdi_prev"]) & (v["pdi"] > v["mdi"]) & tv,
        "ADX(14)>25 且 +DI 今日上穿 −DI、成交金額≥1億", "adx")
    add("C24", "短期超跌（5日跌幅後10%）", 60, (v["ret5_pct"] <= 0.10) & tv,
        "近5日漲跌幅在當日可評估股票最低10%、成交金額≥1億（短期反轉）", "reversal")
    add("C25", "第二階段突破（Weinstein 縮短版）", 71,
        (v["ma60"] >= v["ma60_prev10"]) & (C > v["ma60"]) & (C > v["hh60_prior"]) & (V >= 2.0 * v["avgv20_prior"]) & tv,
        "MA60 走平或向上（≥10日前）、收盤>MA60、收盤突破前60日最高、量≥前20日均量2倍、成交金額≥1億（原版30週均線）", "weinstein")
    add("C26", "三白兵（超跌後）", 60,
        v["up3"] & (v["c_prev"].notna()) & (C < v["ma60"]) & (C > v["ma20"]) & tv,
        "連3日紅K收高，今日收盤站上MA20但仍在MA60之下（低檔轉強）、成交金額≥1億", "3ws")
    add("C27", "多頭排列＋量增", 60,
        (C > v["ma5"]) & (v["ma5"] > v["ma20"]) & (v["ma20"] > v["ma60"]) & (V >= 1.5 * v["avgv20_prior"]) & tv,
        "M2 條件＋量≥前20日均量1.5倍", "ma-align-vol")
    add("C28", "站上季線（廣泛）", 60, (C > v["ma60"]) & tv,
        "收盤>MA60、成交金額≥1億（最寬的趨勢篩選，供分級參考）", "ma60")

    # ===== 新候選：月營收／財報＋行情（狀態版：最新可得資料每天都適用） =====
    rh = v["rv_rev_high"] == True
    add("C29", "營收創高＋站上季線（每日）", 60, rh & (C > v["ma60"]) & tv,
        "最新可得月營收創12個月新高（rev_12m_high-v1）、收盤>MA60、成交金額≥1億", "rev-high")
    add("C30", "營收連3月年增≥20%（每日）", 60, (v["rv_yoy3_ge20"] == True) & (C > v["ma20"]) & tv,
        "最新可得3個月營收年增率都≥20%、收盤>MA20、成交金額≥1億", "rev-yoy")
    add("C31", "營收年增加速（每日）", 60, (v["rv_yoy_accel"] == True) & (C > v["ma20"]) & tv,
        "最新3個月年增率逐月上升且最新>0、收盤>MA20、成交金額≥1億", "rev-accel")
    add("C32", "累計營收年增≥20%＋多頭", 60, (v["rv_cum_yoy"] >= 20) & up_trend & tv,
        "最新可得累計營收年增≥20%、收盤>MA20>MA60、成交金額≥1億", "rev-cum")
    add("C33", "營收創高＋20日突破", 61, rh & (C > v["hh20_prior"]) & (V >= 1.5 * v["avgv20_prior"]) & tv,
        "最新可得月營收創12個月新高、收盤突破前20日最高、量≥前20日均量1.5倍、成交金額≥1億", "rev-high-bo")
    add("C34", "EPS年增≥25%＋多頭（CANSLIM C）", 60, (v["fn_eps_yoy_ge25"] == True) & up_trend & tv,
        "最新可得單季EPS>0且≥去年同季×1.25、收盤>MA20>MA60、成交金額≥1億", "canslim-c")
    add("C35", "CANSLIM 簡化（EPS＋近高＋強勢＋突破）", 121,
        (v["fn_eps_yoy_ge25"] == True) & (C >= 0.85 * v["hh120_incl"]) & (v["rs60_pct"] >= 0.80) & (C > v["hh20_prior"])
        & (V >= 1.4 * v["avgv50_prior"]) & tv,
        "單季EPS年增≥25%、收盤≥120日最高×0.85、60日漲幅前20%、收盤突破前20日最高、量≥前50日均量1.4倍、成交金額≥1億", "canslim")
    add("C36", "毛利率年增＋多頭", 60, (v["fn_gm_up"] == True) & up_trend & tv,
        "最新可得單季毛利率高於去年同季、收盤>MA20>MA60、成交金額≥1億", "gm")
    add("C37", "營收創高＋EPS年增＋多頭", 60, rh & (v["fn_eps_yoy_pos"] == True) & up_trend & tv,
        "最新月營收創12個月新高、最新單季EPS>0且>去年同季、收盤>MA20>MA60、成交金額≥1億", "rev-eps")
    return M
