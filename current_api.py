import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

CONVERT_URL = "https://api.exchangerate.host/convert"
# Документация exchangerate.host: список валют — endpoint /list (не /symbols — там 404).
LIST_URL = "https://api.exchangerate.host/list"

# Загружается через load_symbols(); код валюты → человекочитаемое имя (из API).
symbols: dict[str, str] = {}


def _access_key() -> str | None:
    return os.getenv("CURRENCY_ACCESS_KEY")


def load_symbols() -> tuple[bool, str]:
    """
    Загрузка всех поддерживаемых валют: GET .../list?access_key=...
    Ответ содержит поле "currencies" { "USD": "United States Dollar", ... }.
    Заполняет глобальный словарь symbols.
    """
    global symbols
    key = _access_key()
    if not key:
        return False, "Ключ API не настроен. Добавьте CURRENCY_ACCESS_KEY в .env."

    try:
        response = requests.get(
            LIST_URL, params={"access_key": key}, timeout=60
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    except requests.RequestException as e:
        return False, f"Не удалось загрузить список валют: {e}"

    if not data.get("success"):
        info = data.get("error") or data.get("message") or data
        return False, f"Сервис отклонил запрос list: {info}"

    raw = data.get("currencies") or data.get("symbols")
    if not isinstance(raw, dict):
        return False, "В ответе API нет объекта currencies/symbols."

    out: dict[str, str] = {}
    for code, meta in raw.items():
        c = str(code).upper().strip()
        if len(c) != 3:
            continue
        if isinstance(meta, dict):
            name = str(
                meta.get("description")
                or meta.get("full_name")
                or meta.get("code")
                or c
            )
        else:
            name = str(meta) if meta else c
        out[c] = name

    if not out:
        return False, "Список валют из API пуст."

    symbols.clear()
    symbols.update(out)
    print("DEBUG symbols loaded:", len(symbols))
    print("DEBUG EUR exists:", "EUR" in symbols)
    return True, ""


def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict[str, Any]:
    params = {
        "access_key": _access_key(),
        "from": from_currency.upper().strip(),
        "to": to_currency.upper().strip(),
        "amount": amount,
    }
    response = requests.get(CONVERT_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def convert_amount(
    amount: float, from_currency: str, to_currency: str
) -> tuple[bool, float | str]:
    """
    Успех и результат конвертации, либо сообщение об ошибке для пользователя.
    """
    key = _access_key()
    if not key:
        return False, "Ключ API не настроен. Добавьте CURRENCY_ACCESS_KEY в .env."

    fc = from_currency.upper().strip()
    tc = to_currency.upper().strip()
    if len(fc) != 3 or len(tc) != 3:
        return False, "Код валюты должен быть из трёх букв (например, RUB, EUR)."

    try:
        data = convert_currency(amount, fc, tc)
    except requests.RequestException:
        return False, "Не удалось связаться с сервисом курсов. Попробуйте позже."

    if not data.get("success"):
        info = data.get("error") or data.get("message") or data
        return False, f"Сервис курсов отклонил запрос: {info}"

    result = data.get("result")
    if result is None:
        return False, "В ответе API нет поля с результатом. Проверьте валютную пару."

    try:
        return True, float(result)
    except (TypeError, ValueError):
        return False, "Некорректный числовой результат в ответе API."


def convert_amount_with_meta(
    amount: float, from_currency: str, to_currency: str
) -> tuple[bool, dict[str, Any] | str]:
    """
    Конвертация с метаданными курса:
    - result: итоговая сумма
    - rate: курс from -> to
    - rate_date: дата курса из ответа API, если есть
    """
    key = _access_key()
    if not key:
        return False, "Ключ API не настроен. Добавьте CURRENCY_ACCESS_KEY в .env."

    fc = from_currency.upper().strip()
    tc = to_currency.upper().strip()
    if len(fc) != 3 or len(tc) != 3:
        return False, "Код валюты должен быть из трёх букв (например, RUB, EUR)."

    try:
        data = convert_currency(amount, fc, tc)
    except requests.RequestException:
        return False, "Не удалось связаться с сервисом курсов. Попробуйте позже."

    if not data.get("success"):
        info = data.get("error") or data.get("message") or data
        return False, f"Сервис курсов отклонил запрос: {info}"

    result = data.get("result")
    if result is None:
        return False, "В ответе API нет поля с результатом. Проверьте валютную пару."

    rate_raw = None
    info = data.get("info")
    if isinstance(info, dict):
        rate_raw = info.get("quote") or info.get("rate")
    if rate_raw is None:
        query = data.get("query")
        if isinstance(query, dict):
            amount_raw = query.get("amount")
            if amount_raw is not None:
                try:
                    amount_value = float(amount_raw)
                    result_value = float(result)
                    if amount_value:
                        rate_raw = result_value / amount_value
                except (TypeError, ValueError):
                    rate_raw = None

    try:
        return True, {
            "result": float(result),
            "rate": float(rate_raw) if rate_raw is not None else None,
            "rate_date": str(data.get("date") or ""),
        }
    except (TypeError, ValueError):
        return False, "Некорректные числовые данные в ответе API."


def rate_home_per_one_dest(home: str, dest: str) -> tuple[bool, float | str]:
    """Сколько единиц домашней валюты за 1 единицу валюты пребывания."""
    return convert_amount(1.0, dest, home)


if __name__ == "__main__":
    load_symbols()
    print(f"symbols count: {len(symbols)}")
    print(convert_currency(100, "EUR", "USD"))
