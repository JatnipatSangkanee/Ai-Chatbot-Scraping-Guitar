import sys
import os
import json
import logging
from functools import lru_cache
import time
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from selenium import webdriver
import chromedriver_autoinstaller
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from neo4j import GraphDatabase
from linebot import LineBotApi, WebhookHandler
from linebot.models import FlexSendMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, BubbleContainer, BoxComponent, TextComponent, ImageComponent, ButtonComponent, URIAction
from linebot.exceptions import InvalidSignatureError
from sentence_transformers import SentenceTransformer, util
import faiss
import numpy as np
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

# LINE API credentials
ACCESS_TOKEN = '/1KhrjmLLx0kGUAfFHllqCzDSKCvvddJ00CRbrYn4KRb+aeK/yLnL+Viu75LHXbBHfhq+yTV20XGyPNBle9Axy5VFJxMgGo2TosPgf10V9TiAaVWkuG+teX5NsZlmBMMpUioSOHZ+vUFeE0R9YfZngdB04t89/1O/w1cDnyilFU='
SECRET = '734f5114c2311c942789b2a6569d98b1'

line_bot_api = LineBotApi(ACCESS_TOKEN)
handler = WebhookHandler(SECRET)

# Neo4j connection setup
NEO4J_URI = "neo4j://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "xxxxxxxxxxxx"

# Initialize Neo4j driver
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# Load SentenceTransformer model
model = SentenceTransformer('sentence-transformers/distiluse-base-multilingual-cased-v2')

# Predefined categories and URLs
categories = {
    "Acoustic Guitar": 10,
    "Acoustic Electric": 11,
    "Electric Guitar": 24,
    "Ukulele": 43,
    "Bass Guitar": 28,
    "Classic Guitar": 46,
    "12 String Guitar": 50,
    "Left Hand Guitar": 56
}

url_map = {
    "acoustic_guitar": "https://www.music.co.th/products-category/acoustic-guitar-10/",
    "acoustic_electric_guitar": "https://www.music.co.th/products-category/acoustic-electric-11/",
    "bass_guitar": "https://www.music.co.th/products-category/bass-guitar-28/",
    "electric_guitar": "https://www.music.co.th/products-category/electric-guitar-24/",
    "ukulele": "https://www.music.co.th/products-category/ukulele-43/",
    "classic_guitar": "https://www.music.co.th/products-category/classic-guitar-46/",
    "left_hand_guitar": "https://www.music.co.th/products-category/left-hand-guitar-56/",
    "12_string_guitar": "https://www.music.co.th/products-category/12-string-guitar-50/",
    "acoustic_effect": "https://www.music.co.th/products-category/acoustic-effect-23/",
    "bass_effect": "https://www.music.co.th/products-category/bass-effect-22/",
    "electric_effect": "https://www.music.co.th/products-category/electric-effect-7/",
}

# Intent phrases for FAISS search
intent_phrases = [
    "ดูเมนู",
    "เรียงราคาจากน้อยไปมาก",
    "เรียงราคาจากมากไปน้อย",
    "ไม่",
    "ค้นหา",
    "กีตาร์",
    "Acoustic Effect",
    "Bass Effect",
    "Electirc Effect",
    "สวัสดี"
]

def setup_chrome_driver():
    try:
        chromedriver_autoinstaller.install()
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36')
        
        service = Service()
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e:
        logging.error(f"Failed to initialize Chrome driver: {e}")
        return None

@lru_cache(maxsize=100)
def cached_scrape_guitar_data(url):
    try:
        driver = setup_chrome_driver()
        if driver is None:
            return TextSendMessage(text="ขออภัย เกิดข้อผิดพลาดในการเชื่อมต่อ")

        driver.get(url)
        driver.implicitly_wait(5)
        html = driver.page_source
        driver.quit()

        soup = BeautifulSoup(html, "html.parser")
        job_elements = soup.find_all("div", {"class": "product-list-item-wrapper"})

        guitars = []
        for job_element in job_elements:
            try:
                title_name = job_element.find("span", class_="product-title-name").text.strip()

                sale_price_element = job_element.find("div", class_="price-has-sale")
                if sale_price_element:
                    title_saleprice = sale_price_element.text.strip()
                    full_price_element = job_element.find("small", class_="product__info__price__undiscounted")
                    title_fullprice = full_price_element.text.strip() if full_price_element else title_saleprice
                else:
                    regular_price_element = job_element.find("div", class_="product-list-item-price d-flex")
                    if regular_price_element and regular_price_element.find("div"):
                        title_saleprice = regular_price_element.find("div").text.strip()
                        title_fullprice = title_saleprice
                    else:
                        title_saleprice = title_fullprice = "N/A"

                img_element = job_element.find("img")
                img_url = img_element.get("data-src") or img_element.get("src") if img_element else "N/A"

                product_link = job_element.find("a", class_="product-link link--clean")["href"]
                full_product_url = f"https://www.music.co.th{product_link}"

                guitars.append({
                    'Name': title_name,
                    'Saleprice': title_saleprice,
                    'Fullprice': title_fullprice,
                    'Image': img_url,
                    'ProductLink': full_product_url
                })
            except Exception as e:
                logging.error(f"Error processing guitar element: {e}")
                continue

        if guitars:
            return create_flex_message(guitars[:6])
        else:
            return TextSendMessage(text="ไม่พบผลลัพธ์สำหรับหมวดหมู่ที่เลือก")

    except Exception as e:
        logging.error(f"Error during scraping: {e}")
        return TextSendMessage(text="ขออภัย เกิดข้อผิดพลาดในการค้นหาข้อมูล")

def create_flex_message(guitar_list):
    bubbles = []
    for guitar in guitar_list:
        bubble = BubbleContainer(
            body=BoxComponent(
                layout='vertical',
                contents=[
                    TextComponent(text=guitar['Name'], weight='bold', size='lg'),
                    ImageComponent(url=guitar['Image'], size='full', aspect_ratio="20:13", aspect_mode="cover"),
                    BoxComponent(
                        layout='vertical',
                        margin='md',
                        contents=[
                            TextComponent(text=f"Sale Price: {guitar['Saleprice']}", size='sm', color='#FF0000'),
                            TextComponent(text=f"Full Price: {guitar['Fullprice']}", size='sm', color='#AAAAAA')
                        ]
                    ),
                    ButtonComponent(
                        action=URIAction(label='รายละเอียดเพิ่มเติม', uri=guitar['ProductLink']),
                        style='primary'
                    )
                ]
            )
        )
        bubbles.append(bubble)

    return FlexSendMessage(
        alt_text="Guitar List",
        contents={
            "type": "carousel",
            "contents": bubbles
        }
    )

def create_menu_quick_reply():
    menu_items = ["กีตาร์", "เอฟเฟคกีตาร์", "อื่นๆ(พิมพ์หาเอาเอง)"]
    quick_reply_buttons = [
        QuickReplyButton(action=MessageAction(label=item[:20], text=item[:20]))
        for item in menu_items if item and len(item.strip()) > 0
    ]
    
    if not quick_reply_buttons:
        return TextSendMessage(text="ไม่พบเมนูในขณะนี้")

    return TextSendMessage(
        text="กรุณาเลือกเมนู:",
        quick_reply=QuickReply(items=quick_reply_buttons)
    )

def create_guitar_category_quick_reply():
    guitar_categories = [
        "12 String Guitar",
        "Acoustic Electric",
        "Acoustic Guitar",
        "Bass Guitar",
        "Classic Guitar",
        "Electric Guitar",
        "Left Hand Guitar",
        "Ukulele"
    ]
    
    quick_reply_buttons = [
        QuickReplyButton(action=MessageAction(label=category, text=category))
        for category in guitar_categories
    ]

    quick_reply = QuickReply(items=quick_reply_buttons)
    return TextSendMessage(text="กรุณาเลือกประเภทกีต้าร์:", quick_reply=quick_reply)

def create_guitar_effect_quick_reply():
    guitar_effect = [
    "Acoustic Effect",
    "Bass Effect",
    "Electirc Effect",
    ]
    
    quick_reply_buttons = [
        QuickReplyButton(action=MessageAction(label=category, text=category))
        for category in guitar_effect
    ]

    quick_reply = QuickReply(items=quick_reply_buttons)
    return TextSendMessage(text="กรุณาเลือกประเภทเอฟเฟคกีต้าร์:", quick_reply=quick_reply)

def get_greeting():
    try:
        with driver.session() as session:
            result = session.run("MATCH (n:Greeting) RETURN n.name as name, n.msg_reply as reply")
            greeting = result.single()
            return greeting["reply"] if greeting else "สวัสดีครับ"
    except Exception as e:
        logging.error(f"Error getting greeting: {e}")
        return "สวัสดีครับ"

def save_chat_history(user_id, user_message, bot_message):
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (u:User {id: $user_id})  
                CREATE (c:Chat {message: $user_message, reply: $bot_message, timestamp: datetime()})
                CREATE (b:Bot {message: $bot_message, timestamp: datetime()})
                MERGE (u)-[:SENT]->(c)
                MERGE (c)-[:REPLIED_WITH]->(b)
                """,
                user_id=user_id, user_message=user_message, bot_message=bot_message
            )
        logging.info(f"Saved chat history: UserID: {user_id}, UserMessage: {user_message}, BotReply: {bot_message}")
    except Exception as e:
        logging.error(f"Error saving chat history: {e}")

def get_latest_search_query(user_id):
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (u:User {id: $user_id})-[:SENT]->(c:Chat)
                WHERE c.message CONTAINS 'ค้นหา'
                RETURN c.message AS last_search
                ORDER BY c.timestamp DESC
                LIMIT 1
                """,
                user_id=user_id
            )
            search_message = result.single()
            if search_message:
                return search_message["last_search"].replace("ค้นหา", "").strip()
    except Exception as e:
        logging.error(f"Error getting latest search query: {e}")
    return None

def faiss_search(user_input):
    try:
        search_vector = model.encode([user_input])
        faiss.normalize_L2(search_vector)
        
        vectors = model.encode(intent_phrases)
        vector_dimension = vectors.shape[1]
        index = faiss.IndexFlatL2(vector_dimension)
        faiss.normalize_L2(vectors)
        index.add(vectors)
        
        distances, indices = index.search(search_vector, k=1)
        
        distance_threshold = 0.5
        if distances[0][0] < distance_threshold:
            return intent_phrases[indices[0][0]]
    except Exception as e:
        logging.error(f"Error in FAISS search: {e}")
    return 'unknown'
def llama_change(bot_response):
    try:
        OLLAMA_API_URL = "http://localhost:11434/api/generate"
        headers = {"Content-Type": "application/json"}
        payload = {"model": "supachai/llama-3-typhoon-v1.5", "prompt": f"Generate a response: {bot_response}", "stream": False}
        
        response = requests.post(OLLAMA_API_URL, headers=headers, data=json.dumps(payload))
        
        if response.status_code == 200:
            response_data = response.json()
            return response_data.get("response", bot_response)
        else:
            return bot_response  # ถ้า OLLAMA ใช้งานไม่ได้ ให้ใช้ข้อความเดิม
    except Exception as e:
        return bot_response  # fallback ใช้ข้อความเดิมถ้าเกิดข้อผิดพลาด



def scrape_guitar_data(sort_url=None, keyword=None):
    if sort_url:
        url = sort_url
    elif keyword:
        url = f"https://www.music.co.th/search/?q={keyword}"
    else:
        return TextSendMessage(text="ไม่พบลิงก์หรือคำค้นหา")  # Return error message if both are missing

    # Setup Chrome driver using the defined setup function
    driver = setup_chrome_driver()
    if not driver:
        return TextSendMessage(text="ขออภัย เกิดข้อผิดพลาดในการเชื่อมต่อ")

    driver.get(url)
    driver.implicitly_wait(5)
    html = driver.page_source
    driver.quit()

    soup = BeautifulSoup(html, "html.parser")
    job_elements = soup.find_all("div", {"class": "product-list-item-wrapper"})

    guitars = []
    for job_element in job_elements:
        title_name = job_element.find("span", class_="product-title-name").text.strip()

        sale_price_element = job_element.find("div", class_="price-has-sale")
        if sale_price_element:
            title_saleprice = sale_price_element.text.strip()
            full_price_element = job_element.find("small", class_="product__info__price__undiscounted")
            if full_price_element:
                title_fullprice = full_price_element.text.strip()
            else:
                title_fullprice = title_saleprice
        else:
            regular_price_element = job_element.find("div", class_="product-list-item-price d-flex")
            if regular_price_element:
                price_text_element = regular_price_element.find("div")
                if price_text_element:
                    title_saleprice = price_text_element.text.strip()
                    title_fullprice = title_saleprice
                else:
                    title_saleprice = "N/A"
                    title_fullprice = "N/A"
            else:
                title_saleprice = "N/A"
                title_fullprice = "N/A"

        img_element = job_element.find("img")
        img_url = img_element.get("data-src") or img_element.get("src") if img_element else "N/A"

        product_link = job_element.find("a", class_="product-link link--clean")["href"]
        full_product_url = f"https://www.music.co.th{product_link}"

        guitars.append({
            'Name': title_name,
            'Saleprice': title_saleprice,
            'Fullprice': title_fullprice,
            'Image': img_url,
            'ProductLink': full_product_url
        })

    if guitars:
        return create_flex_message(guitars[:5])
    else:
        return TextSendMessage(text="ไม่พบผลลัพธ์สำหรับหมวดหมู่ที่เลือกครับคุณลูกค้าสุดเท่")



def create_category_quick_reply():
    quick_reply_buttons = [
        QuickReplyButton(action=MessageAction(label=category, text=category))
        for category in categories.keys()
    ]

    quick_reply = QuickReply(items=quick_reply_buttons)
    return TextSendMessage(text="กรุณาเลือกหมวดหมู่ครับคุณลูกค้าสุดเท่:", quick_reply=quick_reply)
def handle_category_selection(category, reply_token):
    # ค้นหาจาก URL ที่กำหนดใน url_map
    category_url = url_map.get(category.lower().replace(" ", "_"), None)
    if category_url:
        products = scrape_guitar_data(sort_url=category_url)
        flex_message = FlexSendMessage(
            alt_text=f"{category} Products",
            contents=create_flex_message(products)
        )
        line_bot_api.reply_message(reply_token, flex_message)
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบหมวดหมู่นี้"))

def handle_search_by_keyword(keyword, reply_token):
    # สร้าง URL ค้นหาตามคำสำคัญ (keyword)
    search_url = f"https://www.music.co.th/search/?q={keyword}"
    products = scrape_guitar_data(sort_url=search_url)
    
    flex_message = FlexSendMessage(
        alt_text=f"Search results for {keyword}",
        contents=create_flex_message(products)
    )
    line_bot_api.reply_message(reply_token, flex_message)

@app.route("/", methods=['POST'])
def linebot():
    body = request.get_data(as_text=True)
    logging.info(f"Received webhook: {body}")
    
    try:
        json_data = json.loads(body)
        msg = json_data['events'][0]['message']['text']
        tk = json_data['events'][0]['replyToken']
        user_id = json_data['events'][0]['source']['userId']

        logging.info(f"Processing message: {msg} from user: {user_id}")

        matched_intent = faiss_search(msg)

        if matched_intent.lower() in ["ดูเมนู","ดูเมนูใหม่"]:
            response = create_menu_quick_reply()
            bot_message = "ส่งรายการเมนู"
            adjusted_message = llama_change(bot_message)  # ปรับข้อความโดย llama_change
            line_bot_api.reply_message(tk, response)
            save_chat_history(user_id, msg, adjusted_message)

        elif matched_intent.lower() in ['hello', 'hi', 'สวัสดี']:
            greeting_message = get_greeting()
            quick_reply = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="ดูเมนู", text="ดูเมนู")),
                QuickReplyButton(action=MessageAction(label="ไม่", text="ไม่"))
            ])
            bot_message = f"{greeting_message}\nคุณต้องการดูตัวอย่างสิ่งที่น่าสนใจไหม"
            adjusted_message = llama_change(bot_message)  # ปรับข้อความโดย llama_change
            line_bot_api.reply_message(tk, TextSendMessage(text=adjusted_message, quick_reply=quick_reply))
            save_chat_history(user_id, msg, adjusted_message)

        elif msg == "กีตาร์":
            response = create_guitar_category_quick_reply()
            line_bot_api.reply_message(tk, response)
            save_chat_history(user_id, msg, "ส่งหมวดหมู่กีต้าร์")

        elif msg == "เอฟเฟคกีตาร์":
            response = create_guitar_effect_quick_reply()
            line_bot_api.reply_message(tk, response)
            save_chat_history(user_id, msg, "ส่งหมวดหมู่เอฟเฟคกีต้าร์")

        elif msg == "Acoustic Guitar":
            products = scrape_guitar_data(sort_url=url_map["acoustic_guitar"])  # Use map URL directly
            line_bot_api.reply_message(tk, products)

        elif msg == "Acoustic Electric":
            products = scrape_guitar_data(sort_url=url_map["acoustic_electric_guitar"])
            line_bot_api.reply_message(tk, products)

        elif msg == "Electric Guitar":
            products = scrape_guitar_data(sort_url=url_map["electric_guitar"])
            line_bot_api.reply_message(tk, products)

        elif msg == "Bass Guitar":
            products = scrape_guitar_data(sort_url=url_map["bass_guitar"])
            line_bot_api.reply_message(tk, products)

        elif msg == "Ukulele":
            products = scrape_guitar_data(sort_url=url_map["ukulele"])
            line_bot_api.reply_message(tk, products)

        elif msg == "Classic Guitar":
            products = scrape_guitar_data(sort_url=url_map["classic_guitar"])
            line_bot_api.reply_message(tk, products)

        elif msg == "Left Hand Guitar":
            products = scrape_guitar_data(sort_url=url_map["left_hand_guitar"])
            line_bot_api.reply_message(tk, products)

        elif msg == "12 String Guitar":
            products = scrape_guitar_data(sort_url=url_map["12_string_guitar"])
            line_bot_api.reply_message(tk, products)
        elif msg == "Acoustic Effect":
            products = scrape_guitar_data(sort_url=url_map["Acoustic Effect"])
            line_bot_api.reply_message(tk, products)
        elif msg == "Bass Effect":
            products = scrape_guitar_data(sort_url=url_map["Bass Effect"])
            line_bot_api.reply_message(tk, products)
        elif msg == "Electric Guitar":
            products = scrape_guitar_data(sort_url=url_map["Electric Guitar"])
            line_bot_api.reply_message(tk, products)
        elif msg == "อื่นๆ(พิมพ์หาเอาเอง)":
            bot_message = "โอเคครับ หากคุณต้องการค้นหาอะไรเองเพิ่มเติม กรุณาพิมพ์คำว่า 'ค้นหา'ตามด้วยสิ่งที่ต้องการหาครับคุณลูกค้าสุดเท่"
            line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
            save_chat_history(user_id, msg, bot_message)
        elif msg == "ไม่":
            bot_message = "โอเคครับ หากคุณต้องการค้นหาอะไรเองเพิ่มเติม กรุณาพิมพ์คำว่า 'ค้นหา'ตามด้วยสิ่งที่ต้องการหาครับคุณลูกค้าสุดเท่"
            line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
            save_chat_history(user_id, msg, bot_message)

        elif matched_intent == "เรียงราคาจากน้อยไปมาก":
            last_search = get_latest_search_query(user_id)
            if last_search:
                sort_url = f"https://www.music.co.th/search/?q={last_search}&sort_by=price_amount"
                products = cached_scrape_guitar_data(sort_url)
                bot_message = f"แสดงผลการค้นหาสำหรับ '{last_search}' เรียงราคาจากน้อยไปมาก"
                line_bot_api.reply_message(tk, products)
                save_chat_history(user_id, msg, bot_message)
            else:
                bot_message = "ไม่พบการค้นหาก่อนหน้านี้ กรุณาทำการค้นหาใหม่ครับคุณลูกค้าสุดเท่"
                line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
                save_chat_history(user_id, msg, bot_message)

        elif matched_intent == "เรียงราคาจากมากไปน้อย":
            last_search = get_latest_search_query(user_id)
            if last_search:
                sort_url = f"https://www.music.co.th/search/?q={last_search}&sort_by=-price_amount"
                products = cached_scrape_guitar_data(sort_url)
                bot_message = f"แสดงผลการค้นหาสำหรับ '{last_search}' เรียงราคาจากมากไปน้อย"
                line_bot_api.reply_message(tk, products)
                save_chat_history(user_id, msg, bot_message)
            else:
                bot_message = "ไม่พบการค้นหาก่อนหน้านี้ กรุณาทำการค้นหาใหม่"
                line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
                save_chat_history(user_id, msg, bot_message)

        elif matched_intent == "ค้นหา":
            keyword = msg.replace("ค้นหา", "").strip()
            if keyword:
                save_chat_history(user_id, msg, f"ค้นหา {keyword}")
                response = create_guitar_category_quick_reply()
                line_bot_api.reply_message(tk, response)
                logging.info(f"Sent category quick reply for keyword: {keyword}")
            else:
                bot_message = "กรุณาระบุคำค้นหากีต้าร์ที่คุณต้องการค้นหา"
                line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))

        elif msg in categories:
            # กรณีที่ผู้ใช้เลือกหมวดหมู่จาก quick reply
            category_id = categories[msg]
            logging.info(f"User selected category: {msg} with ID: {category_id}")
            
            # ตรวจสอบประวัติการค้นหาล่าสุด
            last_search_query = get_latest_search_query(user_id)
            
            if last_search_query:
                # หากมีคำค้นหาล่าสุด แสดงผลลัพธ์รวมกับหมวดหมู่ที่เลือกในรูปแบบ URL ใหม่
                logging.info(f"Searching with previous query: {last_search_query} in category {category_id}")
                search_url = f"https://www.music.co.th/search/?q={last_search_query}&category_id={category_id}"
                flex_message = scrape_guitar_data(sort_url=search_url)
                
                if isinstance(flex_message, FlexSendMessage):
                    logging.info(f"Sending search results for category: {msg} with query: {last_search_query}")
                    line_bot_api.reply_message(tk, flex_message)
                else:
                    logging.info("No products found for this search")
                    bot_message = "ไม่มีสินค้าในหมวดหมู่นี้"
                    line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
            
            else:
                # หากไม่มีคำค้นหาล่าสุด แสดงผลลัพธ์ของหมวดหมู่ที่เลือกเพียงอย่างเดียว
                logging.info(f"Displaying products in category: {msg} without specific search query")
                category_url = url_map.get(msg.lower().replace(" ", "_"))
                
                if category_url:
                    flex_message = scrape_guitar_data(sort_url=category_url)
                    if isinstance(flex_message, FlexSendMessage):
                        logging.info(f"Sending products for category: {msg}")
                        line_bot_api.reply_message(tk, flex_message)
                    else:
                        logging.info("No products found for this category")
                        bot_message = "ไม่มีสินค้าในหมวดหมู่นี้"
                        line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
                else:
                    logging.error(f"Category URL not found for {msg}")
                    bot_message = "ไม่พบหมวดหมู่นี้"
                    line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
        else:
            # กรณีอื่น ๆ ที่ไม่ได้เลือกหมวดหมู่ที่รู้จัก
            bot_message = "ขออภัย ฉันไม่เข้าใจคำสั่งนี้ กรุณาลองใหม่อีกครั้งครับคุณลูกค้าสุดเท่"
            line_bot_api.reply_message(tk, TextSendMessage(text=bot_message))
            save_chat_history(user_id, msg, bot_message)


    except InvalidSignatureError:
        logging.error("Invalid signature. Check your channel secret and access token.")
    except Exception as e:
        logging.error(f"Error: {e}")
    return 'OK'

if __name__ == '__main__':
    app.run(debug=True)  

#กรุงโรมอันนี้ต้องรอดดดดดดดด