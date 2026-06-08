import os

from comment_generator_prompt import comment_generator_prompt
from schedule_loader import format_subreddit_rules


def render_prompt(
    *,
    subreddit_name: str,
    subreddit_rules: list[dict] | str,
    post_title: str,
    post_body: str,
    upvote_count,
    comment_count,
) -> str:
    rules_text = (
        subreddit_rules
        if isinstance(subreddit_rules, str)
        else format_subreddit_rules(subreddit_rules)
    )

    replacements = {
        "{{subreddit_name}}": subreddit_name or "",
        "{{subreddit_rules}}": rules_text,
        "{{post_title}}": post_title or "",
        "{{post_body}}": post_body or "",
        "{{upvote_count}}": str(upvote_count if upvote_count is not None else 0),
        "{{comment_count}}": str(comment_count if comment_count is not None else 0),
    }

    prompt = comment_generator_prompt
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def get_llm_config() -> tuple[str, str]:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "gemini":
        return provider, os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return provider, os.getenv("OPENAI_MODEL", "gpt-5.4")


def generate_comment(prompt: str) -> str:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()

    if provider == "gemini":
        return _generate_gemini(prompt)
    if provider == "openai":
        return _generate_openai(prompt)

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}. Use 'openai' or 'gemini'.")


def _generate_openai(prompt: str) -> str:
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set in .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


def _generate_gemini(prompt: str) -> str:
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in .env")

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    return (response.text or "").strip()
