import asyncio
from browser_use import Agent, ChatOpenAI, BrowserProfile

async def main():
    llm = ChatOpenAI(
        model="anti-browser",
        base_url="http://127.0.0.1:20128/v1",
        api_key="local-router",
    )
    profile = BrowserProfile(
        headless=True,
        chromium_sandbox=False,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
        user_data_dir="/var/lib/anti-agents/browser-profile",
    )
    agent = Agent(
        task=(
            "Open https://example.com, read the page title and main heading. "
            "Do not click external links. Do not submit anything."
        ),
        llm=llm,
        browser_profile=profile,
        use_vision=False,
    )
    result = await agent.run(max_steps=5)
    print("--- RESULT ---")
    print(result.final_result())

if __name__ == '__main__':
    asyncio.run(main())
