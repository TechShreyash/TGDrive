from cloudscraper import create_scraper
import aiohttp, requests


async def main():
    print("aiohttp")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/"
        ) as response:
            print(response.headers)

    print("cloudscraper")

    s = create_scraper()
    response = s.get(
        "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/", stream=True
    )
    print(response.headers)

    print("requests")
    response = requests.get(
        "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/"
    )
    print(response.headers)


import asyncio

# asyncio.run(main())
