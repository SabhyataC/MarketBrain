import base64
from app.model_client import client, MODEL_NAME

with open("samples/Shelf2.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model=MODEL_NAME,
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "List every product visible on this shelf. For each give: name, brand, and any size or variant on the label. Return JSON only."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }],
)
print(resp.choices[0].message.content)
print(resp.usage)