import os
from app.calllog import timed
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL_NAME = os.getenv("MODEL_NAME", "muse-spark-1.1")

client = OpenAI(
    api_key=os.environ["MODEL_API_KEY"],
    base_url=os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1"),
)


def _guard():
    from app.killswitch import guard_models  # imported late: killswitch needs the database module
    guard_models()


def ask(prompt: str) -> str:
    _guard()
    with timed("text", MODEL_NAME) as t:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        t["resp"] = resp
    return resp.choices[0].message.content

import base64


def ask_with_image(prompt: str, image_path: str) -> str:
    _guard()
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    with timed("vision", MODEL_NAME) as t:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        )
        t["resp"] = resp
    return resp.choices[0].message.content

def generate_image(prompt: str, out_path: str, size: str = "1024x1024") -> str:
    _guard()
    model = os.getenv("IMAGE_MODEL_NAME", "muse-image-1.0")
    with timed("image_generate", model) as t:
        resp = client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            response_format="b64_json",
            extra_body={"output_format": "png"},
        )
        t["resp"] = resp
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(resp.data[0].b64_json))
    return out_path

def edit_image(prompt: str, reference_path: str, out_path: str, size: str = "1024x1024") -> str:
    _guard()
    model = os.getenv("IMAGE_MODEL_NAME", "muse-image-1.0")
    with open(reference_path, "rb") as f, timed("image_edit", model) as t:
        resp = client.images.edit(
            model=model,
            image=f,
            prompt=prompt,
            size=size,
            response_format="b64_json",
            extra_body={"output_format": "png"},
        )
        t["resp"] = resp
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(resp.data[0].b64_json))
    return out_path