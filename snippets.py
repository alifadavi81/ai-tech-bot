from __future__ import annotations
import random
from typing import Optional, Dict, List

CODE_SNIPPETS: List[Dict] = [
    {
        "title": "پایتون | کشف سریع Bottleneck با cProfile",
        "tags": ["python", "perf"],
        "code": (
            "import cProfile, pstats, io\n\n"
            "def heavy():\n    return sum(i*i for i in range(200_000))\n\n"
            "pr = cProfile.Profile()\npr.enable()\nheavy()\npr.disable()\n\n"
            "s = io.StringIO()\n"
            "pstats.Stats(pr, stream=s).sort_stats('cumtime').print_stats(10)\n"
            "print(s.getvalue())\n"
        ),
        "desc": "پروفایل ساده برای پیدا کردن توابع کند.",
    },
    {
        "title": "پایتون | Async HTTP با httpx و تجمع نتایج",
        "tags": ["python", "async", "network"],
        "code": (
            "import asyncio, httpx\n\n"
            "URLS = ['https://httpbin.org/delay/1', 'https://httpbin.org/delay/2']\n\n"
            "async def fetch(client, url):\n    r = await client.get(url, timeout=10)\n    return url, r.status_code\n\n"
            "async def main():\n    async with httpx.AsyncClient() as client:\n        results = await asyncio.gather(*(fetch(client,u) for u in URLS))\n        for url, status in results:\n            print(url, status)\n\n"
            "asyncio.run(main())\n"
        ),
        "desc": "الگوی تمیز برای درخواست‌های همزمان.",
    },
    {
        "title": "رباتیک | فیلتر میانگین متحرک برای نویز سنسور",
        "tags": ["robotics", "sensors", "python"],
        "code": (
            "from collections import deque\n\n"
            "class MovingAvg:\n    def __init__(self, k=5):\n        self.k = k\n        self.q = deque(maxlen=k)\n\n"
            "    def update(self, x):\n        self.q.append(x)\n        return sum(self.q) / len(self.q)\n\n"
            "flt = MovingAvg(10)\n"
            "for x in [1,2,100,3,4,5]:\n    print(flt.update(x))\n"
        ),
        "desc": "صاف‌سازی ساده‌ی قرائت سنسور برای کنترلرها.",
    },
    {
        "title": "IoT | ارسال Telemetry به MQTT (paho-mqtt)",
        "tags": ["iot", "mqtt", "python"],
        "code": (
            "import json, time, random\nimport paho.mqtt.client as mqtt\n\n"
            "client = mqtt.Client()\nclient.connect('broker.hivemq.com', 1883, 60)\n\n"
            "topic = 'demo/iot/telemetry'\nfor _ in range(5):\n    payload = {'t': time.time(), 'temp': round(20+random.random()*5,2)}\n    client.publish(topic, json.dumps(payload), qos=0, retain=False)\n    print('sent:', payload)\n    time.sleep(1)\n\n"
            "client.disconnect()\n"
        ),
        "desc": "ارسال داده به بروکر MQTT عمومی برای تست.",
    },
]

def pick_code(tag: Optional[str] = None) -> Optional[Dict]:
    pool = CODE_SNIPPETS if not tag else [c for c in CODE_SNIPPETS if tag in c["tags"]]
    if not pool:
        return None
    return random.choice(pool)

def code_to_text(sn: Dict) -> str:
    return (
        f"💡 <b>{sn['title']}</b>\n"
        f"{sn['desc']}\n\n"
        f"<pre><code>{sn['code']}</code></pre>"
    )
