"""
this file will demonstrate how to use instructor 
to restrict the output of structured data from llm
"""

from pydantic import BaseModel, Field
import instructor
import openai

# ========== 正确：客户端初始化时填入 key + base_url ==========
llm_client = openai.OpenAI(
    api_key="ak_2FI70d9lI8qe0w37sP0pS1Q33On70",
    base_url="https://api.longcat.chat/openai"   # LongCat对应的兼容接口地址，必须加 /v1
)

# 包装instructor
client = instructor.from_openai(llm_client)

class UserInfo(BaseModel):
    name: str = Field(description="用户名称")
    age: int = Field(description="用户年龄")
    email: str = Field(description="用户邮箱")

res = client.chat.completions.create(
    model="LongCat-2.0",
    messages=[
        {"role": "user", "content": "提取信息：李四，28岁，lisi@test.com"}
    ],
    response_model=UserInfo,
    max_retries=2   # 格式解析失败自动重试，强烈建议加上
)

print(res.model_dump())
print(res.name, res.age)
