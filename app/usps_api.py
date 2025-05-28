from urllib.parse import urljoin
import asyncio
import datetime
import time
import sys
import html
import xmltodict
from redis import asyncio as aioredis
import httpx
from typing import Dict, Any, Optional

from . import config


USPS_API_URL = "https://services.usps.com"
USPS_SERVICE_API_BASE = "https://iv.usps.com/ivws_api/informedvisapi/"
_USPS_STDZ_URL = "https://api.usps.com/addresses/v3/address"
_TOKEN: dict[str, Any] = {"value": None, "expires": 0}
_TOKEN_URL = "https://api.usps.com/oauth2/v3/token"

headers = {'Content-type': 'application/json'}

redis_client = aioredis.Redis(host=config.REDIS_HOST, port=6379, db=0)
httpx_client = httpx.AsyncClient(timeout=15)


async def generate_token_usps(username: str,
                              passwd: str):
    data = {
        "username": username,
        "password": passwd,
        "grant_type": "authorization",
        "response_type": "token",
        "scope": "user.info.ereg,iv1.apis",
        "client_id": "687b8a36-db61-42f7-83f7-11c79bf7785e"}
    try:
        response = await httpx_client.post(urljoin(USPS_API_URL, "oauth/authenticate"), json=data, headers=headers)
    except httpx.HTTPError as err:
        return {"error": "HTTPError", "error_description": str(err)}
    try:
        response = response.json()
    except ValueError:
        return {"error": "ValueError", "error_description": "Invalid JSON"}
    return response


async def refresh_token_usps(refresh_token: str):
    data = {
        "refresh_token": refresh_token,
        "grant_type": "authorization",
        "response_type": "token",
        "scope": "user.info.ereg,iv1.apis"
    }
    try:
        response = await httpx_client.post(urljoin(USPS_API_URL, "oauth/token"), json=data, headers=headers)
    except httpx.HTTPError as err:
        return {"error": "HTTPError", "error_description": str(err)}
    return response.json()


async def token_maintain():
    access_token = await redis_client.get("usps_access_token")
    next_refresh_time = await redis_client.get("usps_token_nextrefresh")
    refresh_token = await redis_client.get("usps_refresh_token")
    now = datetime.datetime.now()
    if next_refresh_time is not None:
        next_refresh_time = datetime.datetime.fromtimestamp(
            float(next_refresh_time.decode('utf-8')))
    if next_refresh_time is None or now > next_refresh_time:
        resp = await generate_token_usps(config.BSG_USERNAME, config.BSG_PASSWD)
        if "error" in resp:
            return
        token_type = resp['token_type']
        access_token = resp['access_token']
        refresh_token = resp['refresh_token']
        expires_in = int(resp['expires_in'])
        refresh_token = resp['refresh_token']
        await redis_client.set("usps_access_token", access_token)
        await redis_client.set("usps_token_nextrefresh", time.time() + expires_in/2.0)
        await redis_client.set("usps_refresh_token", refresh_token)
        await redis_client.set("usps_token_type", token_type)
    else:
        refresh_token = refresh_token.decode('utf-8')
        resp = await refresh_token_usps(refresh_token)
        if "error" in resp:
            return
        token_type = resp['token_type']
        access_token = resp['access_token']
        expires_in = int(resp['expires_in'])
        await redis_client.set("usps_access_token", access_token)
        await redis_client.set("usps_token_type", token_type)
        await redis_client.set("usps_token_nextrefresh", time.time() + expires_in/2.0)


async def get_authorization_header():
    next_refresh_time = await redis_client.get("usps_token_nextrefresh")
    if next_refresh_time is not None:
        next_refresh_time = datetime.datetime.fromtimestamp(
            float(next_refresh_time.decode('utf-8')))
    now = datetime.datetime.now()
    if next_refresh_time is None or now > next_refresh_time:
        await token_maintain()
    access_token = (await redis_client.get("usps_access_token")).decode('utf-8')
    token_type = (await redis_client.get("usps_token_type")).decode('utf-8')
    headers_local = dict()
    headers_local["Authorization"] = token_type + " " + access_token
    return headers_local


async def get_piece_tracking(imb: str):
    url = urljoin(USPS_SERVICE_API_BASE, "api/mt/get/piece/imb/" + imb)
    try:
        response = await httpx_client.get(url, headers=await get_authorization_header())
    except httpx.HTTPError as err:
        return {"error": "HTTPError", "error_description": str(err)}
    return response.json()

async def _get_usps_token(client: httpx.AsyncClient) -> str:
    """Return a cached Bearer token or fetch a fresh one."""
    if _TOKEN["value"] and _TOKEN["expires"] - time.time() > 60:
        return _TOKEN["value"]

    data = {
        "grant_type": "client_credentials",
        "client_id":     config.USPS_CLIENT_ID,
        "client_secret": config.USPS_CLIENT_SECRET,
    }
    r = await client.post(_TOKEN_URL, data=data, timeout=20)
    r.raise_for_status()
    j = r.json()

    _TOKEN["value"]   = j["access_token"]
    _TOKEN["expires"] = time.time() + j.get("expires_in", 1800)
    return _TOKEN["value"]

async def get_USPS_standardized_address(address: Dict[str, str]) -> Dict[str, Any]:
    """
    Validate & standardize an address using USPS Addresses 3.0.

    `address` keys accepted (all lower-case):
        firmname, address1 (secondary/unit), address2 (street),
        city, state*, zip5, zip4, urbanization

    Returns either:
        {'error': '…'}   on failure
        {standardized-fields…} on success
    """
    # ---- map our field names -> USPS query params --------------------------
    params: dict[str, str] = {}

    if address.get("firmname"):
        params["firm"] = address["firmname"]

    # USPS: streetAddress = primary line  (our address2)
    params["streetAddress"] = address.get("address2", "")

    # USPS: secondaryAddress = apartment / suite (our address1)
    if address.get("address1"):
        params["secondaryAddress"] = address["address1"]

    if address.get("city"):
        params["city"] = address["city"]

    if not address.get("state"):
        return {"error": "state is required"}
    params["state"] = address["state"]

    if address.get("urbanization"):
        params["urbanization"] = address["urbanization"]

    if address.get("zip5"):
        params["ZIPCode"] = address["zip5"]

    if address.get("zip4"):
        params["ZIPPlus4"] = address["zip4"]

    # -----------------------------------------------------------------------
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token = await _get_usps_token(client)
        except httpx.HTTPError as err:
            return {"error": "TokenError", "error_description": str(err)}

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            r = await client.get(_USPS_STDZ_URL, params=params, headers=headers)
        except Exception as e:
            return {"error": str(e)}
    # -----------------------------------------------------------------------
    payload = r.json()
    
    if "error" in payload:
        # pick first error, strip HTML entities
        msg = str(payload["error"])
        return {"error": html.unescape(msg)}
    address = payload.get("address", {})
    standardized_address = {
        "firmname":  payload.get("firm", ""),
        "address1":  address.get("secondaryAddress", ""), # address1 is address line 2
        "address2":  address.get("streetAddress", ""), # address2 is address line 1 (street)
        "city":      address.get("city", ""),
        "state":     address.get("state", ""),
        "zip5":      address.get("ZIPCode", ""),
        "zip4":      address.get("ZIPPlus4", ""),
        "dp":        payload.get("additionalInfo", {}).get("deliveryPoint", ""),
    }
    return standardized_address


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    print(loop.run_until_complete(get_piece_tracking(sys.argv[1])))
