#!/usr/bin/env python3
"""
Telegram Bot for Indian Vehicle RC Details Lookup
Authorized Penetration Testing Tool
Target: Parivahan Vahan NR Portal

Capabilities:
  - Scrape Parivahan portal (captcha solving via OCR/2captcha)
  - Lookup vehicle details by registration number
  - Return owner info, vehicle specs, registration details
  - Export RC details as PDF report

Legal: For authorized security testing only.
"""

import os
import re
import io
import json
import logging
import asyncio
import tempfile
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests
from bs4 import BeautifulSoup
from PIL import Image
import cv2
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import mm

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ============================================================
# CONFIGURATION
# ============================================================

# --- Telegram Bot ---
BOT_TOKEN = "7586456125:AAFYNsCrPYiudaNWlBrzPT2orWeYrn4qlMg"  # Replace with your bot token from @BotFather

# --- Optional: Paid API (if you get one later) ---
PAID_API_KEY = None     # e.g., "sk_live_xxxx"
PAID_API_ENDPOINT = None  # e.g., "https://api.attestr.com/api/v2/public/checkx/rc"

# --- Captcha solving ---
CAPTCHA_API_KEY = None   # 2captcha API key (more reliable than OCR)
USE_OCR = True           # Set False if using 2captcha

# --- Session ---
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# PARIVAHAN SCRAPER ENGINE
# ============================================================

class ParivahanScraper:
    """
    Scrapes vehicle details from the Parivahan/Vahan portal.
    Multi-source approach: old rcdlstatus portal, NR services, paid API.
    """

    OLD_RCDL_HOME = "https://parivahan.gov.in/rcdlstatus/"
    OLD_RCDL_POST = "https://parivahan.gov.in/rcdlstatus/vahan/rcDlHome.xhtml"
    NR_SERVICES = "https://vahan.parivahan.gov.in/nrservices"
    CITIZEN_LOGIN = f"{NR_SERVICES}/faces/user/citizen/citizenlogin.xhtml"
    SEARCH_PAGE = f"{NR_SERVICES}/faces/user/searchstatus.xhtml"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })

    # ------------------------------------------------------------------
    # CAPTCHA SOLVING
    # ------------------------------------------------------------------

    def _solve_captcha_ocr(self, captcha_image_bytes: bytes) -> Optional[str]:
        """Solve text-based captcha using OCR."""
        try:
            img = Image.open(io.BytesIO(captcha_image_bytes))
            img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

            # Try digits-only first
            custom_config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789'
            text = pytesseract.image_to_string(thresh, config=custom_config).strip()
            if text and text.isdigit():
                return text

            # Try math expressions
            text = pytesseract.image_to_string(thresh, config='--oem 3 --psm 7').strip()
            return self._solve_math_captcha(text)

        except Exception as e:
            logger.error(f"OCR captcha failed: {e}")
            return None

    def _solve_math_captcha(self, text: str) -> Optional[str]:
        """Solve math-based captchas like 'Smallest No 12, 45' or '23 + 45 ='."""
        text = " ".join(text.split())

        # Smallest No X, Y
        m = re.match(r'Smallest No (\d+), ?(\d+)', text, re.IGNORECASE)
        if m:
            return str(min(int(m.group(1)), int(m.group(2))))

        # Greater No X, Y
        m = re.match(r'Greater No (\d+), ?(\d+)', text, re.IGNORECASE)
        if m:
            return str(max(int(m.group(1)), int(m.group(2))))

        # X + Y =
        m = re.match(r'(\d+)\s*\+\s*(\d+)\s*=', text)
        if m:
            return str(int(m.group(1)) + int(m.group(2)))

        # X - Y =
        m = re.match(r'(\d+)\s*\-\s*(\d+)\s*=', text)
        if m:
            return str(int(m.group(1)) - int(m.group(2)))

        # X * Y =
        m = re.match(r'(\d+)\s*\*\s*(\d+)\s*=', text)
        if m:
            return str(int(m.group(1)) * int(m.group(2)))

        return None

    def _solve_captcha(self, captcha_image_bytes: bytes) -> Optional[str]:
        """Solve captcha using configured method."""
        if not USE_OCR and CAPTCHA_API_KEY:
            return self._solve_captcha_2captcha(captcha_image_bytes)
        return self._solve_captcha_ocr(captcha_image_bytes)

    def _solve_captcha_2captcha(self, captcha_image_bytes: bytes) -> Optional[str]:
        """Solve captcha using 2captcha service."""
        if not CAPTCHA_API_KEY:
            return None
        try:
            files = {"file": ("captcha.jpg", captcha_image_bytes, "image/jpeg")}
            data = {"key": CAPTCHA_API_KEY, "method": "post", "json": 1}
            resp = requests.post("https://2captcha.com/in.php", data=data, files=files)
            result = resp.json()
            if result.get("status") != 1:
                return None

            captcha_id = result["request"]
            for _ in range(30):
                import time
                time.sleep(3)
                poll = requests.get(
                    "https://2captcha.com/res.php",
                    params={"key": CAPTCHA_API_KEY, "action": "get", "id": captcha_id, "json": 1},
                )
                poll_result = poll.json()
                if poll_result.get("status") == 1:
                    return poll_result["request"]
        except Exception as e:
            logger.error(f"2captcha failed: {e}")
        return None

    # ------------------------------------------------------------------
    # OLD RCDL STATUS PORTAL
    # ------------------------------------------------------------------

    def lookup_via_old_portal(self, reg_number: str) -> Optional[Dict[str, Any]]:
        """
        Try the old rcdlstatus portal (JSF-based with viewstate + math captcha).
        Still functional for many states.
        """
        reg_number = reg_number.upper().replace(" ", "")

        try:
            # Step 1: GET home page, extract viewstate + captcha image URL
            resp = self.session.get(self.OLD_RCDL_HOME, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")

            viewstate_tag = soup.find("input", {"name": "javax.faces.ViewState"})
            if not viewstate_tag:
                logger.warning("No ViewState found on old portal")
                return None
            viewstate_val = viewstate_tag.get("value", "")

            captcha_img = soup.find("img", {"id": "rcDlHome:capcha"})
            if not captcha_img:
                logger.warning("No captcha image on old portal")
                return None

            captcha_src = captcha_img.get("src", "")
            if captcha_src.startswith("/"):
                captcha_src = "https://parivahan.gov.in" + captcha_src
            elif captcha_src.startswith(".."):
                captcha_src = "https://parivahan.gov.in" + captcha_src[2:]

            # Step 2: Download and solve captcha
            captcha_resp = self.session.get(captcha_src, timeout=REQUEST_TIMEOUT)
            captcha_text = self._solve_captcha(captcha_resp.content)
            if not captcha_text:
                logger.error("Failed to solve captcha")
                return None

            logger.info(f"Solved captcha: {captcha_text}")

            # Step 3: Submit the form via JSF partial/ajax
            form_data = {
                "javax.faces.ViewState": viewstate_val,
                "rcDlHome": "rcDlHome",
                "rcDlHome:regn_no1_exact": reg_number,
                "rcDlHome:txt_ALPHA_NUMERIC": captcha_text,
                "rcDlHome:j_idt30": "rcDlHome:j_idt30",
                "javax.faces.source": "rcDlHome:j_idt30",
                "javax.faces.partial.event": "click",
                "javax.faces.partial.execute": (
                    "rcDlHome:j_idt30 rcDlHome:regn_no1_exact rcDlHome:txt_ALPHA_NUMERIC"
                ),
                "javax.faces.partial.render": (
                    "rcDlHome:resultPanel rcDlHome:userMessages "
                    "rcDlHome:capcha rcDlHome:txt_ALPHA_NUMERIC"
                ),
                "javax.faces.partial.ajax": "true",
            }

            headers = {
                "Faces-Request": "partial/ajax",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.OLD_RCDL_HOME,
            }

            post_resp = self.session.post(
                self.OLD_RCDL_POST, data=form_data, headers=headers, timeout=REQUEST_TIMEOUT
            )

            # Step 4: Parse response
            result = self._parse_jsf_partial_response(post_resp.text, reg_number)
            if result:
                return result

            result = self._parse_html_response(post_resp.text, reg_number)
            if result:
                return result

            # Check if the response contains an error message
            if "Invalid" in post_resp.text or "not exist" in post_resp.text.lower():
                logger.warning(f"Vehicle {reg_number} not found or invalid captcha")
                return {"registration_number": reg_number, "error": "Vehicle not found or invalid captcha"}

            return None

        except Exception as e:
            logger.error(f"Old portal lookup failed: {e}")
            return None

    def _parse_jsf_partial_response(self, text: str, reg_number: str) -> Optional[Dict[str, Any]]:
        """Parse JSF partial/ajax response XML."""
        try:
            soup = BeautifulSoup(text, "html.parser")

            # Find the result panel update
            result_html = None
            for update in soup.find_all("update"):
                uid = update.get("id", "")
                if "resultPanel" in uid:
                    result_html = update.decode_contents()
                    break

            if not result_html:
                return None

            result_soup = BeautifulSoup(result_html, "html.parser")
            return self._extract_rc_details(result_soup, reg_number)

        except Exception as e:
            logger.error(f"Parse JSF partial response error: {e}")
            return None

    def _parse_html_response(self, text: str, reg_number: str) -> Optional[Dict[str, Any]]:
        """Parse full HTML response."""
        try:
            soup = BeautifulSoup(text, "html.parser")
            return self._extract_rc_details(soup, reg_number)
        except Exception as e:
            logger.error(f"Parse HTML response error: {e}")
            return None

    def _extract_rc_details(self, soup: BeautifulSoup, reg_number: str) -> Optional[Dict[str, Any]]:
        """Extract RC details from parsed HTML with title-attribute mapping and table parsing."""
        details = {
            "registration_number": reg_number,
            "source": "parivahan_portal",
            "timestamp": datetime.now().isoformat(),
        }

        field_mapping = {
            "Registration No": "registration_number",
            "Registration Number": "registration_number",
            "RC Status": "rc_status",
            "Vehicle Class": "vehicle_class",
            "Model": "model_name",
            "Model Name": "model_name",
            "Manufacturer": "manufacturer",
            "Manufacturer Name": "manufacturer",
            "Registering Authority": "registering_authority",
            "Owner Name": "owner_name",
            "Owner": "owner_name",
            "Father Name": "father_name",
            "Father's Name": "father_name",
            "Present Address": "owner_address",
            "Permanent Address": "permanent_address",
            "Address": "owner_address",
            "Fuel": "fuel_type",
            "Fuel Type": "fuel_type",
            "Engine No": "engine_number",
            "Engine Number": "engine_number",
            "Chassis No": "chassis_number",
            "Chassis Number": "chassis_number",
            "Cubic Capacity": "cubic_capacity",
            "CC": "cubic_capacity",
            "Seating Capacity": "seating_capacity",
            "Unladen Weight": "unladen_weight",
            "Gross Weight": "gross_weight",
            "Year": "manufacturing_year",
            "Manufacturing Year": "manufacturing_year",
            "Regn Date": "registration_date",
            "Registration Date": "registration_date",
            "Valid From": "validity_from",
            "Validity From": "validity_from",
            "Valid Upto": "validity_to",
            "Validity To": "validity_to",
            "Insurance": "insurance_details",
            "Insurance Expiry": "insurance_expiry",
            "PUCC": "pucc_details",
            "PUCC Upto": "pucc_upto",
            "Tax Paid Upto": "tax_paid_upto",
            "Tax Upto": "tax_paid_upto",
            "Hypothecation": "hypothecation",
            "Blacklist": "blacklist_status",
            "NOC": "noc_details",
            "Mobile": "mobile_number",
            "Mobile No": "mobile_number",
        }

        # Method 1: Cells with title attributes
        for cell in soup.find_all(["td", "th", "span", "div", "label"]):
            title = (cell.get("title", "") or cell.get_text(strip=True) or "").strip()
            for field_name, key in field_mapping.items():
                if field_name.lower() in title.lower():
                    # Next sibling or next td
                    value_cell = cell.find_next(["td", "span", "div"])
                    if value_cell:
                        val = value_cell.get_text(strip=True)
                        if val:
                            details[key] = val

        # Method 2: Parse tables row by row
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True)
                    value = cells[1].get_text(strip=True)
                    for field_name, key in field_mapping.items():
                        if field_name.lower() in label.lower() and value:
                            details[key] = value

        # Method 3: Look for definition lists / label-value divs
        for label in soup.find_all(["label", "span", "div"]):
            text = label.get_text(strip=True)
            for field_name, key in field_mapping.items():
                if field_name.lower() in text.lower() and ":" in text:
                    parts = text.split(":", 1)
                    if len(parts) == 2 and parts[1].strip():
                        details[key] = parts[1].strip()

        # Validate we got something meaningful
        if len(details) <= 3:
            return None

        return details

    # ------------------------------------------------------------------
    # PAID API
    # ------------------------------------------------------------------

    def lookup_via_paid_api(self, reg_number: str) -> Optional[Dict[str, Any]]:
        """Lookup via configured paid API."""
        if not PAID_API_KEY or not PAID_API_ENDPOINT:
            return None
        try:
            headers = {
                "Authorization": f"Bearer {PAID_API_KEY}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                PAID_API_ENDPOINT,
                json={"reg": reg_number},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "registration_number": reg_number,
                    "source": "paid_api",
                    "timestamp": datetime.now().isoformat(),
                    **data.get("data", data),
                }
            logger.error(f"Paid API error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Paid API failed: {e}")
        return None

    # ------------------------------------------------------------------
    # NR SERVICES (citizen login required)
    # ------------------------------------------------------------------

    def login_nr_services(self, mobile: str, otp: str) -> bool:
        """Login to NR services with mobile + OTP. Maintains session cookies."""
        try:
            # Get login page
            resp = self.session.get(self.CITIZEN_LOGIN, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract viewstate
            viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
            if not viewstate:
                return False

            # Login step 1: submit mobile
            form_data = {
                "javax.faces.ViewState": viewstate.get("value", ""),
                "loginContainer:loginMobileNo": mobile,
                "loginContainer:j_idt28": "loginContainer:j_idt28",
                "javax.faces.source": "loginContainer:j_idt28",
                "javax.faces.partial.event": "click",
                "javax.faces.partial.execute": "loginContainer:j_idt28 loginContainer:loginMobileNo",
                "javax.faces.partial.render": "loginContainer:loginPanel",
                "javax.faces.partial.ajax": "true",
            }
            headers = {
                "Faces-Request": "partial/ajax",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.CITIZEN_LOGIN,
            }
            self.session.post(self.CITIZEN_LOGIN, data=form_data, headers=headers, timeout=REQUEST_TIMEOUT)

            # Login step 2: submit OTP
            form_data.update({
                "loginContainer:otpMobile": otp,
                "loginContainer:j_idt39": "loginContainer:j_idt39",
                "javax.faces.source": "loginContainer:j_idt39",
                "javax.faces.partial.execute": "loginContainer:j_idt39 loginContainer:otpMobile",
            })
            resp = self.session.post(self.CITIZEN_LOGIN, data=form_data, headers=headers, timeout=REQUEST_TIMEOUT)

            return "Welcome" in resp.text or "logout" in resp.text.lower()
        except Exception as e:
            logger.error(f"NR login failed: {e}")
            return False

    def lookup_via_nr_services(self, reg_number: str) -> Optional[Dict[str, Any]]:
        """Lookup via NR services portal (must be logged in)."""
        try:
            resp = self.session.get(self.SEARCH_PAGE, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")

            viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
            if not viewstate:
                return None

            form_data = {
                "javax.faces.ViewState": viewstate.get("value", ""),
                "regn_no1_exact": reg_number,
                "javax.faces.source": "j_idt35",
                "j_idt35": "j_idt35",
                "javax.faces.partial.event": "click",
                "javax.faces.partial.execute": "j_idt35 regn_no1_exact",
                "javax.faces.partial.render": "rcDetailsPanel",
                "javax.faces.partial.ajax": "true",
            }
            headers = {
                "Faces-Request": "partial/ajax",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.SEARCH_PAGE,
            }
            resp = self.session.post(self.SEARCH_PAGE, data=form_data, headers=headers, timeout=REQUEST_TIMEOUT)
            return self._parse_jsf_partial_response(resp.text, reg_number)
        except Exception as e:
            logger.error(f"NR lookup failed: {e}")
            return None

    # ------------------------------------------------------------------
    # MAIN LOOKUP
    # ------------------------------------------------------------------

    def lookup(self, reg_number: str) -> Optional[Dict[str, Any]]:
        """
        Main lookup method. Tries multiple sources in priority order:
        1. Paid API (if configured)
        2. Old rcdlstatus portal (most reliable without login)
        3. NR services (requires prior login)
        """
        reg_number = reg_number.upper().replace(" ", "")

        # 1. Paid API
        result = self.lookup_via_paid_api(reg_number)
        if result:
            return result

        # 2. Old portal
        result = self.lookup_via_old_portal(reg_number)
        if result:
            return result

        logger.warning(f"All lookup methods failed for {reg_number}")
        return None


# ============================================================
# PDF REPORT GENERATOR
# ============================================================

class PDFGenerator:
    """Generate professionally formatted RC details PDF."""

    @staticmethod
    def generate(details: Dict[str, Any]) -> bytes:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            rightMargin=20*mm, leftMargin=20*mm,
            topMargin=20*mm, bottomMargin=20*mm,
        )
        styles = getSampleStyleSheet()
        elements = []

        # Title
        elements.append(Paragraph("Vehicle Registration Certificate (RC) Details", styles["Title"]))
        elements.append(Spacer(1, 8))

        # Metadata
        meta = [
            f"Generated: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}",
            f"Source: {details.get('source', 'Unknown')}",
        ]
        if "error" in details:
            meta.append(f"Status: ERROR - {details['error']}")
        for line in meta:
            elements.append(Paragraph(line, styles["Normal"]))
        elements.append(Spacer(1, 12))

        if "error" in details:
            doc.build(elements)
            buffer.seek(0)
            return buffer.getvalue()

        # Field display order
        display_fields = [
            ("Registration Number", "registration_number"),
            ("RC Status", "rc_status"),
            ("Owner Name", "owner_name"),
            ("Father's Name", "father_name"),
            ("Present Address", "owner_address"),
            ("Permanent Address", "permanent_address"),
            ("Mobile Number", "mobile_number"),
            ("Vehicle Class", "vehicle_class"),
            ("Manufacturer", "manufacturer"),
            ("Model Name", "model_name"),
            ("Fuel Type", "fuel_type"),
            ("Engine Number", "engine_number"),
            ("Chassis Number", "chassis_number"),
            ("Cubic Capacity (cc)", "cubic_capacity"),
            ("Seating Capacity", "seating_capacity"),
            ("Unladen Weight (kg)", "unladen_weight"),
            ("Gross Weight (kg)", "gross_weight"),
            ("Manufacturing Year", "manufacturing_year"),
            ("Registration Date", "registration_date"),
            ("Validity From", "validity_from"),
            ("Validity To", "validity_to"),
            ("Registering Authority", "registering_authority"),
            ("Insurance Details", "insurance_details"),
            ("Insurance Expiry", "insurance_expiry"),
            ("PUCC Details", "pucc_details"),
            ("PUCC Upto", "pucc_upto"),
            ("Tax Paid Upto", "tax_paid_upto"),
            ("Hypothecation", "hypothecation"),
            ("Blacklist Status", "blacklist_status"),
            ("NOC Details", "noc_details"),
        ]

        table_data = [["Field", "Value"]]
        for label, key in display_fields:
            val = details.get(key)
            if val:
                table_data.append([label, str(val)])

        # If no structured fields matched, dump everything
        if len(table_data) <= 1:
            table_data = [["Field", "Value"]]
            for k, v in details.items():
                if k not in ("source", "timestamp", "registration_number"):
                    table_data.append([k.replace("_", " ").title(), str(v)])

        table = Table(table_data, colWidths=[150, 330])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a237e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))

        elements.append(table)
        elements.append(Spacer(1, 20))
        elements.append(Paragraph("<i>Generated by HackerAI - Authorized Penetration Testing Tool</i>", styles["Normal"]))

        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()


# ============================================================
# TELEGRAM BOT HANDLERS
# ============================================================

# Conversation states
MAIN_MENU, WAITING_REG_NUMBER = range(2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    text = (
        f"🚗 *Welcome {user.first_name}!*\n\n"
        "I'm a Vehicle RC Details Lookup Bot for authorized penetration testing.\n\n"
        "*Commands:*\n"
        "🔍 `/lookup` - Lookup vehicle by registration number\n"
        "📄 `/pdf` - Get last result as PDF\n"
        "ℹ️ `/help` - Show help\n\n"
        "*Example:* `MH02CL0555`"
    )
    keyboard = [
        [InlineKeyboardButton("🔍 Lookup Vehicle", callback_data="lookup")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = (
        "🚗 *RC Details Bot - Help*\n\n"
        "This bot fetches vehicle registration details from Indian government "
        "Parivahan/Vahan databases for authorized testing.\n\n"
        "*How to use:*\n"
        "1. Send `/lookup` or click the button\n"
        "2. Enter the vehicle registration number (e.g., DL01AB1234)\n"
        "3. Wait for results\n"
        "4. Use `/pdf` to download as PDF\n\n"
        "*Tip:* The old rcdlstatus portal works best for most states. "
        "Some states may not return mobile numbers due to privacy masking."
    )
    keyboard = [[InlineKeyboardButton("🔍 Lookup Vehicle", callback_data="lookup")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lookup command - start the lookup flow."""
    text = "🔍 *Enter Vehicle Registration Number*\n\nSend me the registration number (e.g., `MH02CL0555` or `DL01AB1234`)"
    await update.message.reply_text(text, parse_mode="Markdown")
    return WAITING_REG_NUMBER


async def handle_reg_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the registration number input and perform lookup."""
    reg_number = update.message.text.strip().upper().replace(" ", "")

    # Validate format
    if not re.match(r'^[A-Z]{2}\d{1,2}[A-Z]{1,2}\d{1,4}$', reg_number):
        await update.message.reply_text(
            "❌ *Invalid format.*\n\n"
            "Use format like: `MH02CL0555` or `DL01AB1234`\n"
            "2 letters state code + 1-2 digit RTO code + 1-2 letters + 1-4 digits",
            parse_mode="Markdown"
        )
        return WAITING_REG_NUMBER

    status_msg = await update.message.reply_text(f"🔍 Searching for `{reg_number}`...\nThis may take a moment.", parse_mode="Markdown")

    # Perform lookup
    scraper: ParivahanScraper = context.bot_data.get("scraper")
    details = scraper.lookup(reg_number)

    if not details or "error" in details:
        error_text = details.get("error", "Vehicle not found or could not be retrieved.")
        await status_msg.edit_text(
            f"❌ *Lookup Failed*\n\n`{reg_number}`\n\n{error_text}\n\n"
            "Possible reasons:\n"
            "• Invalid registration number\n"
            "• Portal captcha could not be solved\n"
            "• Vehicle not in central database\n"
            "• Portal requires citizen login for this state",
            parse_mode="Markdown"
        )
        return MAIN_MENU

    # Store result for PDF generation
    user_id = update.effective_user.id
    context.user_data["last_result"] = details

    # Format result message
    result_text = format_result_message(details)

    keyboard = [
        [InlineKeyboardButton("📄 Download PDF", callback_data="pdf")],
        [InlineKeyboardButton("🔍 Search Another", callback_data="lookup")],
    ]

    await status_msg.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return MAIN_MENU


def format_result_message(details: Dict[str, Any]) -> str:
    """Format RC details as a Telegram message."""
    lines = ["✅ *Vehicle Details Found*\n"]

    # Basic info
    fields = [
        ("registration_number", "🔢 Reg Number"),
        ("rc_status", "📋 RC Status"),
        ("owner_name", "👤 Owner Name"),
        ("father_name", "👤 Father's Name"),
        ("owner_address", "📍 Address"),
        ("mobile_number", "📱 Mobile"),
        ("vehicle_class", "🚗 Class"),
        ("manufacturer", "🏭 Manufacturer"),
        ("model_name", "🚘 Model"),
        ("fuel_type", "⛽ Fuel Type"),
        ("engine_number", "🔧 Engine No"),
        ("chassis_number", "🔩 Chassis No"),
        ("cubic_capacity", "📐 Cubic Capacity"),
        ("seating_capacity", "💺 Seating"),
        ("manufacturing_year", "📅 Year"),
        ("registration_date", "📅 Reg Date"),
        ("validity_from", "📅 Valid From"),
        ("validity_to", "📅 Valid Until"),
        ("registering_authority", "🏛️ RTO"),
        ("insurance_details", "🛡️ Insurance"),
        ("insurance_expiry", "📅 Insurance Expiry"),
        ("tax_paid_upto", "💰 Tax Upto"),
        ("hypothecation", "🏦 Hypothecation"),
        ("blacklist_status", "⚠️ Blacklist Status"),
    ]

    for key, label in fields:
        val = details.get(key)
        if val and str(val).strip() and str(val).strip() not in ("N/A", "0", ""):
            lines.append(f"*{label}:* {val}")

    lines.append(f"\n_Generated: {details.get('timestamp', datetime.now().isoformat())[:19]} | Source: {details.get('source', 'parivahan')}_")
    return "\n".join(lines)


async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pdf command - generate and send PDF."""
    details = context.user_data.get("last_result")
    if not details:
        await update.message.reply_text(
            "❌ No previous lookup result found.\nUse `/lookup` first.",
            parse_mode="Markdown"
        )
        return

    pdf_bytes = PDFGenerator.generate(details)
    reg = details.get("registration_number", "vehicle")

    await update.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=f"RC_Details_{reg}.pdf",
        caption=f"📄 RC Details Report - {reg}",
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    if query.data == "lookup":
        await query.edit_message_text(
            "🔍 *Enter Vehicle Registration Number*\n\nSend me the registration number (e.g., `MH02CL0555`)",
            parse_mode="Markdown"
        )
        return WAITING_REG_NUMBER

    elif query.data == "pdf":
        details = context.user_data.get("last_result")
        if not details:
            await query.edit_message_text(
                "❌ No previous result. Use /lookup first.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Lookup Vehicle", callback_data="lookup")]
                ])
            )
            return

        pdf_bytes = PDFGenerator.generate(details)
        reg = details.get("registration_number", "vehicle")
        await query.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=f"RC_Details_{reg}.pdf",
            caption=f"📄 RC Details Report - {reg}",
        )

    elif query.data == "help":
        text = (
            "🚗 *Help*\n\n"
            "This bot fetches vehicle registration details from Parivahan/Vahan.\n\n"
            "*Commands:*\n"
            "`/start` - Welcome screen\n"
            "`/lookup` - Lookup vehicle\n"
            "`/pdf` - Download last result as PDF\n"
            "`/help` - This message\n\n"
            "*Format:* `MH02CL0555` (State + RTO + Series + Number)"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

    return MAIN_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation."""
    await update.message.reply_text("Cancelled. Use /lookup to start again.")
    return MAIN_MENU


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors."""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("An error occurred. Please try again.")


# ============================================================
# MAIN
# ============================================================

def main():
    """Start the bot."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Set your BOT_TOKEN in the script or as TELEGRAM_BOT_TOKEN env variable.")
        print("   Get it from @BotFather on Telegram.")
        print("   You can also set: export TELEGRAM_BOT_TOKEN='your_token_here'")
        sys.exit(1)

    # Allow env var override
    token = os.environ.get("TELEGRAM_BOT_TOKEN", BOT_TOKEN)

    # Create application
    app = Application.builder().token(token).build()

    # Store scraper in bot_data
    app.bot_data["scraper"] = ParivahanScraper()

    # Conversation handler for lookup flow
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("lookup", lookup_cmd),
            MessageHandler(filters.Regex(r'^[A-Za-z]{2}\d'), handle_reg_number),
            CallbackQueryHandler(button_callback, pattern="^lookup$"),
        ],
        states={
            WAITING_REG_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reg_number),
                CallbackQueryHandler(button_callback),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pdf", pdf_cmd))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)

    print("✅ Bot started. Press Ctrl+C to stop.")
    print(f"   Bot token: {token[:8]}...{token[-4:]}")
    print("   Tip: Set USE_OCR=False and configure CAPTCHA_API_KEY for 2captcha (more reliable)")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import sys
    main()