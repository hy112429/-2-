#!/usr/bin/env python3
"""商品查询引擎脚本（商品查询主界面数据源，NDJSON 事件协议）。

状态说明：
  * source = "mock"：当前是**可控的假数据**（固定随机种子，结果可复现），
    用于打通 UI 全链路。将来接巨量百应等真实开放接口时，
    只需要替换 get_product_data() / get_video_data() 的内部实现，
    输出结构保持不变，UI 无需改动。

CLI：
  python product_query.py --product-id 123456789

输出（每行一条 JSON）：
  {"type":"stage","stage":"…","percent":N}
  {"type":"result","data":{ "source":"mock", "product":{…}, "videos":[…原始全集…] }}

注意：时间范围 / 优质 / 条数筛选由界面**本地完成**（需求明确），
所以这里返回全量视频（发布时间分布在 0~40 天，让过滤有区分度）。
"""

import argparse
import datetime
import json
import random
import sys

SEED = 20261010  # 固定种子 → 假数据可控、可复现，便于验证

PRODUCT = {
    "id": "7076521897453267950",
    "name": "示例保温杯 500ml 便携随行杯",
    "category": "家居生活 > 水具",
    "commission_rate": 20.0,
}

AUTHORS = ["抖音好物测评", "小北选品", "直播间严选", "工厂直供店"]
LICENSES = ["已授权", "未授权", "待确认"]
COLORS = ["#d9e8fc", "#fdebd0", "#e6f7e6", "#fbe3e3", "#e8e3fb"]
TITLES = [
    "实用保温杯日常测评，保温 12 小时实测",
    "夏天喝冰水冬天喝热水，一杯搞定",
    "办公室人手一个的随行杯 | 平价好物",
    "直播间爆款杯子，今天又补货了",
    "车载/通勤两用的保温杯推荐",
    "对比三款保温杯，这款最划算",
    "孩子上学带水必备，防漏设计",
    "大容量 500ml，一天喝水不焦虑",
]


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def get_product_data(product_id: str) -> dict:
    return {**PRODUCT, "id": product_id}


def get_video_data() -> list:
    """生成 40 条可控假视频（发布时间 0~40 天随机分布）。"""
    rng = random.Random(SEED)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    videos = []
    for i in range(1, 41):
        days_ago = rng.randint(0, 40)
        publish = now - datetime.timedelta(days=days_ago)
        videos.append({
            "id": i,
            "title": TITLES[rng.randrange(len(TITLES))] + f" #{i}",
            "author": AUTHORS[rng.randrange(len(AUTHORS))],
            "publish_at": publish.isoformat(timespec="seconds"),
            "published_days_ago": days_ago,
            "pay_amount_yuan": round(rng.uniform(9.9, 129.0), 2),
            "ctr_percent": round(rng.uniform(0.5, 12.0), 2),
            "deal_count": rng.randint(15, 3000),
            "license": LICENSES[rng.randrange(len(LICENSES))],
            # mock 下载源：本地测试服务（联调用，WAF 解除后换真实无水印地址）
            "video_url": f"http://127.0.0.1:8000/v{i}.mp4",
            "cover_color": COLORS[rng.randrange(len(COLORS))],
            "is_premium": i <= 20,  # 前 20 条标记优质，便于过滤演示
        })
    return videos


def main() -> int:
    parser = argparse.ArgumentParser(description="商品查询引擎脚本（mock 数据源）")
    parser.add_argument("--product-id", required=True, help="商品 ID")
    args = parser.parse_args()

    product_id = args.product_id.strip()

    emit({"type": "stage", "stage": "查询商品信息", "percent": 30})
    emit({"type": "log", "level": "info", "message": f"商品 ID：{product_id}（当前为 mock 数据源）"})
    emit({"type": "stage", "stage": "拉取关联视频列表", "percent": 70})
    emit({"type": "stage", "stage": "完成", "percent": 100})
    emit({
        "type": "result",
        "data": {
            "source": "mock",
            "product": get_product_data(product_id),
            "videos": get_video_data(),
        },
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())