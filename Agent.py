import os
import re
import json
import uuid
import requests
from dotenv import load_dotenv
from openai import OpenAI
from IPython.display import Markdown, display, HTML
from langchain_community.utilities import GoogleSerperAPIWrapper
from io import BytesIO
from PIL import Image

# config

load_dotenv(override=True)

openai_api_key = os.getenv("OPENAI_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")
OLLAMA_BASE_URL = "http://localhost:11434/v1"
ollama = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
requests.get('http://localhost:11434').content
model_name = "Qwen3:8B"

FINTO_BASE = "https://finto.day"
finto_email = os.environ.get("FINTO_EMAIL")
finto_password = os.environ.get("FINTO_PASSWORD")

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
gemini = OpenAI(api_key=google_api_key, base_url=GEMINI_BASE_URL)

serper = GoogleSerperAPIWrapper()
serper_images = GoogleSerperAPIWrapper(type="images")

MODEL = "gemini-3.1-flash-lite"

CATEGORY_MAP = {
    "Technology": 1, "Web Development": 2, "Artificial Intelligence": 3, "Gadgets": 4,
    "Business": 5, "Startups": 6, "Finance": 7,
    "Lifestyle": 8, "Health": 9, "Travel": 10,
}

VALID_CATEGORIES = list(CATEGORY_MAP.keys())


# prompt

PROMPT = """
You are a content agent responsible for producing and publishing content 
for our platform. Given a category and subtopic, follow this process 
IN ORDER, using the tools available to you:

Category must be exactly one of these (use the exact spelling/casing):
Technology, Web Development, Artificial Intelligence, Gadgets,
Business, Startups, Finance,
Lifestyle, Health, Travel

1. RESEARCH
   Call web_search tool with a short, specific query (4-6 words) to gather 
   current, factual context on the subtopic within the category. This is 
   to avoid generic or outdated filler — do not skip this step.
   You may call it up to 2 times if the first results are too broad or irrelevant.

2. WRITE CONTENT
   Using the research, write a piece with:
   - title (SEO-friendly, max 70 characters)
   - intro (2-3 sentences)
   - sections (5, each with a heading and body text)
   - conclusion (2-3 sentences)
   - tags (3-5 relevant SEO tags)
   Do not fabricate facts, statistics, or quotes not supported by the research.
   Word count target: word_count words total.

3. SOURCE IMAGES
   Call image_search tool once per needed image (3-5 total):
   - 1 hero/featured image — landscape orientation
   - 2-4 supporting images, one per relevant section - adjust accordingly that it do not end up taking more space the text content
   Only use royalty-free sources. Return the image URL and source name for each.
   If no suitable image is found for a section, skip it rather than 
   inventing a URL.
   - return the URL of each image used

4. ASSEMBLE DRAFT
   Combine the written content and images into a single JSON object 
   matching this structure:
   {
     "title": "", "slug": "", "category": "", "tags": [],
     "meta_description": "", "intro": "",
     "sections": [{"heading": "", "text": "", "image": {"url": "", "source": ""}}],
     "conclusion": "", "featured_image": {"url": "", "source": ""},
     "status": "draft"
   }

5. STOP FOR HUMAN APPROVAL
   Do NOT call publish tool yet. Present the assembled draft as your final 
   response for this turn, clearly labeled, and wait for explicit approval 
   before publishing.

6. PUBLISH (only after approval is given )
   Call publish tool with the approved payload. Set "status" to "draft" or 
   "live" based on what the human specifies.

7. REPORT
   After publish tool returns, report back the URL/ID and a 1-line summary.

Rules:
- Follow the steps in order — do not skip research or jump straight to writing.
- Never call publish tool without explicit human approval in the conversation.
- If any tool call fails, report the error and stop — do not retry more than once.
- Do not fabricate image URLs, facts, or statistics.
"""

#Tool — web_search
def web_search(query: str) -> str:
    """Search the web for the current information on a given query."""
    return serper.run(query)


web_search_json = {
    "name": "web_search",
    "description": "Search the web for current information relevant to the topic",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A short, specific search query to search the web (4-6 words)"
            }
        },
        "required": ["query"],
        "additionalProperties": False
    }
}


# Tool — image_search
def image_search(query):
    """Search for royalty-free images relevant to the query."""
    results = serper_images.results(query)
    images = results.get("images", [])[:5]
    if not images:
        return []
    return [
        {"url": img.get("imageUrl"), "source": img.get("source", "Unknown")}
        for img in images
    ]


image_search_json = {
    "name": "image_search",
    "description": "Search the web for royalty free images on platforms like Unsplash, Pixabay, Pexels, etc. relevant to the query",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A short, specific search query to search for images (4-6 words)"
            }
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

# Tool — publish (finto.day)

def get_csrf_token(session: requests.Session, url:str) -> str:
    resp = session.get(url)
    match = re.search(r'name="_token" value="([^"]+)"', resp.text)
    if not match:
        raise ValueError(f"Could not find CSRF token on {url}")
    return match.group(1)


def login(session: requests.Session) -> None:
    login_url = f"{FINTO_BASE}/writer/login"
    token = get_csrf_token(session, login_url)
    resp = session.post(
        login_url,
        data={"_token": token, "email": finto_email, "password": finto_password},
    )
    resp.raise_for_status()
    if "/writer/login" in resp.url:
        raise ValueError("Login failed — check credentials or CSRF handling")


def download_image(url: str, max_dimension: int = 1200, quality: int = 80) -> bytes:
    """
    Download an image and re-encode it as a compressed JPEG, capped max_dimension on
    it's longest side. Stock photo sources often serve multi-MB originals; building 
    several of those into one multipart upload can trip the platform's max request-body
    size (a 413 error)
    """

    headers = {
        # Some image hosts reject requests carrying the default
        # "python-requests/x.x" User-Agent (basic anti-scraping). A
        # realistic browser UA avoids that class of failure.
        "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    img = Image.open(BytesIO(resp.content)).convert("RGB")
    img.thumbnail((max_dimension, max_dimension))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def format_body_for_finto(payload):
    parts = [payload.get("intro", "")]
    for section in payload.get("sections", []):
        parts.append(f"<h2>{section['heading']}</h2><p>{section['text']}</p>")
    parts.append(f"<p>{payload.get('conclusion', '')}</p>")
    return "".join(parts)


def build_acknowledgement(payload):
    sources = set()
    if payload.get("featured_image", {}).get("source"):
        sources.add(payload["featured_image"]["source"])
    for s in payload.get("sections", []):
        if s.get("image", {}).get("source"):
            sources.add(s["image"]["source"])
    return f"Images sourced from {', '.join(sources)}" if sources else ""


def publish(payload):
    """Publish the content draft to finto.day."""
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)

        session = requests.Session()
        login(session)

        new_article_url = f"{FINTO_BASE}/writer/articles/create"
        token = get_csrf_token(session, new_article_url)

        body_html = format_body_for_finto(payload)

        data = {
            "_token": token,
            "title": payload["title"][:150],
            "short_description": payload.get("meta_description", "")[:255],
            "category_id": CATEGORY_MAP.get(payload.get("category"), ""),
            "body": body_html,
            "meta_keywords": ", ".join(payload.get("tags", [])),
            "meta_description": payload.get("meta_description", "")[:255],
            "meta_content": payload.get("intro", "")[:500],
            "acknowledgement": build_acknowledgement(payload),
            "is_published": "1" if payload.get("status") == "live" else "0",
            "is_full_width_image": "0",
            "image_gallery_layout": "vertical",
            "default_image": "new:0",
        }
        for i, tag in enumerate(payload.get("tags", [])[:5]):
            data[f"tags[{i}]"] = tag

        files = {}
        images = []
        if payload.get("featured_image", {}).get("url"):
            images.append(payload["featured_image"])
        for section in payload.get("sections", []):
            if section.get("image", {}).get("url"):
                images.append(section["image"])

        skipped_images = []
        file_index = 0
        for img in images[:5]:
            try:
                img_bytes = download_image(img["url"])
            except Exception as e:
                # One bad image (blocked, dead link, timeout, etc.) shouldn't
                # sink the whole publish — skip it and keep going.
                skipped_images.append({"url": img["url"], "error": str(e)})
                continue
            files[f"images[{file_index}]"] = (f"image{file_index}.jpg", img_bytes, "image/jpeg")
            data[f"images_alt[{file_index}]"] = img.get("source", "")
            file_index += 1

        resp = session.post(f"{FINTO_BASE}/writer/articles", data=data, files=files)
        resp.raise_for_status()

        return {"success": True, "url": resp.url}

    except Exception as e:
        return {"success": False, "error": str(e)}


publish_json = {
    "name": "publish",
    "description": "Publish the finished content draft to the platform.",
    "parameters": {
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "description": "The final content draft + images to publish"
            }
        },
        "required": ["payload"],
        "additionalProperties": False
    }
}



tools = [
    {"type": "function", "function": web_search_json},
    {"type": "function", "function": image_search_json},
    {"type": "function", "function": publish_json},
]

tools_flow = {
    "web_search": web_search,
    "image_search": image_search,
    "publish": publish,
}


# Agent loop

def agent01(category, subtopic, word_count):
    filled_prompt = PROMPT.replace("word_count", str(word_count))
    messages = [
        {"role": "system", "content": filled_prompt},
        {"role": "user", "content": f"category: {category}, subtopic: {subtopic}"}
    ]
    response = gemini.chat.completions.create(model=MODEL, messages=messages, tools=tools)

    while response.choices[0].finish_reason == "tool_calls":
        message = response.choices[0].message
        messages.append(message)
        for tool_call in message.tool_calls:
            function_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            result = tools_flow[function_name](**args)
            messages.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id})
        response = gemini.chat.completions.create(model=MODEL, messages=messages, tools=tools)

    return response.choices[0].message.content, messages


def review_draft(messages, decision, feedback=None, live=False):
    """
    decision: "approve" or "reject"
    feedback: required if decision == "reject"
    live: only used if decision == "approve" — True = publish live, False = save as draft
    """
    if decision == "reject":
        if not feedback:
            raise ValueError("Feedback is required when rejecting a draft.")
        user_msg = f"Not approved. Please revise the draft based on this feedback: {feedback}"
    elif decision == "approve":
        user_msg = f"Approved. Publish with status = {'live' if live else 'draft'}."
    else:
        raise ValueError("decision must be 'approve' or 'reject'")

    messages.append({"role": "user", "content": user_msg})
    response = gemini.chat.completions.create(model=MODEL, messages=messages, tools=tools)

    while response.choices[0].finish_reason == "tool_calls":
        message = response.choices[0].message
        messages.append(message)
        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            result = tools_flow[fn_name](**args)
            messages.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id})
        response = gemini.chat.completions.create(model=MODEL, messages=messages, tools=tools)

    return response.choices[0].message.content, messages



# Display helpers
def clean_json_string(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1]
        s = s.rsplit("```", 1)[0]
        s = s.strip()
    match = re.search(r'\{.*\}', s, re.DOTALL)
    if match:
        s = match.group(0)
    return s.strip()


def display_draft(draft_json_str):
    if not draft_json_str or not draft_json_str.strip():
        print("Draft is empty — check the tool-call trail in `messages` for what went wrong.")
        return

    cleaned = clean_json_string(draft_json_str)
    if not cleaned:
        print("No JSON object found in draft content. Raw content was:")
        print(repr(draft_json_str))
        return

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"Could not parse draft as JSON: {e}")
        print("Raw content was:")
        print(repr(draft_json_str))
        return

    title = data.get('title') or '(no title)'
    meta_description = data.get('meta_description') or ''
    category = data.get('category') or ''
    tags = data.get('tags') or []
    intro = data.get('intro') or ''
    sections = data.get('sections') or []
    conclusion = data.get('conclusion') or ''
    status = data.get('status') or 'unknown'
    featured = data.get('featured_image') or {}

    all_images = []
    if featured.get('url'):
        all_images.append(featured['url'])
    for section in sections:
        img = section.get('image') or {}
        url = img.get('url')
        if url and url not in all_images:
            all_images.append(url)

    gallery_id = f"gallery_{uuid.uuid4().hex[:8]}"

    if all_images:
        thumbs_html = "".join(
            f'<img src="{url}" class="{gallery_id}-thumb" '
            f'onclick="document.getElementById(\'{gallery_id}-hero\').src=\'{url}\'; '
            f'document.querySelectorAll(\'.{gallery_id}-thumb\').forEach(t=>t.classList.remove(\'active\')); '
            f'this.classList.add(\'active\')" />'
            for url in all_images
        )
        gallery_html = f"""
        <div style="display:flex; gap:12px; margin:16px 0;">
            <div style="flex:1; max-width:600px;">
                <img id="{gallery_id}-hero" src="{all_images[0]}"
                     style="width:100%; border-radius:8px; object-fit:cover; aspect-ratio:16/9;" />
            </div>
            <div style="display:flex; flex-direction:column; gap:8px; width:90px;">
                {thumbs_html}
            </div>
        </div>
        <style>
            .{gallery_id}-thumb {{
                width:100%; height:60px; object-fit:cover; border-radius:6px;
                cursor:pointer; opacity:0.65; border:2px solid transparent;
                transition: all 0.15s ease;
            }}
            .{gallery_id}-thumb:hover {{ opacity:1; }}
            .{gallery_id}-thumb.active {{ opacity:1; border-color:#4a90d9; }}
        </style>
        """
    else:
        gallery_html = "<p><em>(no images sourced for this draft)</em></p>"

    sections_html = ""
    for section in sections:
        heading = section.get('heading') or ''
        text = section.get('text') or ''
        sections_html += f"<h3>{heading}</h3><p>{text}</p>"

    html = f"""
    <div style="font-family:sans-serif; max-width:700px; line-height:1.5;">
        <h1>{title}</h1>
        <p><em>{meta_description}</em></p>
        <p><strong>Category:</strong> {category} | <strong>Tags:</strong> {', '.join(tags)}</p>

        {gallery_html}

        <p>{intro}</p>
        {sections_html}
        <p>{conclusion}</p>
        <hr/>
        <p><strong>Status:</strong> {status}</p>
    </div>
    """

    display(HTML(html))


#  Helper to extract the result of a specific tool from the message history
def get_last_tool_result(messages, tool_name):
    tool_call_id = None
    
    # Find the ID of the last call to the requested tool
    for msg in reversed(messages):
        # Safely get attributes whether msg is a dict or a Pydantic object
        if isinstance(msg, dict):
            role = msg.get("role")
            tool_calls = msg.get("tool_calls")
        else:
            role = getattr(msg, "role", None)
            tool_calls = getattr(msg, "tool_calls", None)
            
        if role == "assistant" and tool_calls:
            for tc in tool_calls:
                if isinstance(tc, dict):
                    tc_name = tc["function"]["name"]
                    tc_id = tc["id"]
                else:
                    tc_name = tc.function.name
                    tc_id = tc.id
                    
                if tc_name == tool_name:
                    tool_call_id = tc_id
                    break
        if tool_call_id:
            break
            
    if not tool_call_id:
        return None
        
    # Find the tool result matching that ID
    for msg in reversed(messages):
        if isinstance(msg, dict):
            role = msg.get("role")
            msg_tc_id = msg.get("tool_call_id")
            content = msg.get("content")
        else:
            role = getattr(msg, "role", None)
            msg_tc_id = getattr(msg, "tool_call_id", None)
            content = getattr(msg, "content", None)
            
        if role == "tool" and msg_tc_id == tool_call_id:
            return json.loads(content)
            
    return None


if __name__ == "__main__":
    draft, messages = agent01(category="Health", subtopic="food and diet in 2026", word_count=800)
    print(draft)

#  revised_draft, messages = review_draft(messages, decision="reject", feedback="...")
    result, messages = review_draft(messages, decision="approve", live=False)
    print(result)