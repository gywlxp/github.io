import requests
import os
import datetime

def main():
    city = os.getenv("CITY", "石家庄")
    token = os.getenv("PUSHPLUS_TOKEN")
    if not token:
        print("错误：未配置PUSHPLUS_TOKEN")
        return 1

    # 1. 抓取中文天气数据（无需注册API Key）
    weather_url = f"http://wttr.in/{city}?format=j1&lang=zh"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(weather_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"抓取天气失败：{e}")
        return 1

    # 2. 解析核心数据
    today = data["weather"][0]
    current = data["current_condition"][0]
    date_str = datetime.datetime.now().strftime("%m月%d日 %A")
    # 星期转中文
    week_map = {"Monday":"周一","Tuesday":"周二","Wednesday":"周三","Thursday":"周四",
                "Friday":"周五","Saturday":"周六","Sunday":"周日"}
    for en, zh in week_map.items():
        date_str = date_str.replace(en, zh)
    
    max_temp = today["maxtempC"]
    min_temp = today["mintempC"]
    noon_weather = today["hourly"][4] # 取中午12点的预报更准确
    weather_desc = noon_weather["lang_zh"][0]["value"] if "lang_zh" in noon_weather else noon_weather["weatherDesc"][0]["value"]
    wind_dir = noon_weather["winddir16Point"]
    wind_speed = int(noon_weather["windspeedKmph"]) // 3.6 # 转成m/s
    humidity = noon_weather["humidity"]
    rain_chance = noon_weather["chanceofrain"]
    temp_now = current["temp_C"]
    feels_like = current["FeelsLikeC"]

    # 3. 自动生成实用提示
    tips = []
    avg_temp = (int(max_temp) + int(min_temp)) / 2
    # 穿衣提示
    if avg_temp < 5:
        tips.append("👕 穿衣建议：天气寒冷，建议穿厚羽绒服、棉服，做好防寒")
    elif avg_temp < 15:
        tips.append("👕 穿衣建议：气温较低，建议穿风衣、厚外套加毛衣")
    elif avg_temp < 25:
        tips.append("👕 穿衣建议：温度适宜，建议穿薄外套、卫衣或长袖衬衫")
    else:
        tips.append("👕 穿衣建议：天气较热，建议穿短袖、薄T恤等清凉衣物")
    # 降水提示
    if int(rain_chance) > 60:
        tips.append("☔ 降水提示：今日降水概率高，出门一定要带伞")
    elif int(rain_chance) > 30:
        tips.append("☔ 降水提示：有较小概率降水，可以随身备一把伞")
    else:
        tips.append("☔ 降水提示：今日无降水，无需带伞")
    # 温差提示
    if int(max_temp) - int(min_temp) > 10:
        tips.append("🌡️ 温差提示：今日昼夜温差超过10℃，早晚注意添衣")

    # 4. 拼接推送内容
    title = f"🌤️ {city}今日天气提醒"
    content = f"""
📅 {date_str}
🌡️ 当前温度：{temp_now}°C（体感{feels_like}°C）
🌡️ 今日温度：{min_temp}°C ~ {max_temp}°C
☁️ 天气状况：{weather_desc}
💨 风力：{wind_dir} {wind_speed}m/s
💧 相对湿度：{humidity}%
☔ 降水概率：{rain_chance}%

---
{chr(10).join(tips)}
    """.strip()

    # 5. 推送到微信
    push_url = "http://www.pushplus.plus/send"
    push_data = {
        "token": token,
        "title": title,
        "content": content.replace("\n", ""),
        "template": "html"
    }
    try:
        push_resp = requests.post(push_url, json=push_data, timeout=10)
        push_result = push_resp.json()
        if push_result.get("code") == 200:
            print("推送成功！")
            print("推送内容：\n", content)
            return 0
        else:
            print(f"推送失败：{push_result.get('msg')}")
            return 1
    except Exception as e:
        print(f"推送请求失败：{e}")
        return 1

if __name__ == "__main__":
    exit(main())
