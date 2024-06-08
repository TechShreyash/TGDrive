from cloudscraper import create_scraper
import aiohttp, requests
from aiocfscrape import CloudflareScraper


async def main():

    print("cloudscraper")
    s = create_scraper()
    response = s.get(
        "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/", stream=True
    )
    print(response.headers)

    cookies, headers = s.get_cookie_string(
        "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/"
    )

    print("aiohttp")
    async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
        async with session.get(
            "https://vadapav.mov/f/e6a15357-f5a3-49e9-a887-c5cd0d29fc35/"
        ) as response:
            print(response.headers)
