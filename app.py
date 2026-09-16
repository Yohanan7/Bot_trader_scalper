import json
import os
import re
from datetime import datetime, timezone

import requests
import streamlit as st

try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

st.set_page_config(page_title="Bot Scalper XAU/USD", page_icon="📈", layout="wide")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
MT5_LOGIN = os.getenv("MT5_LOGIN", "").strip()
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "").strip()
MT5_SERVER = os.getenv("MT5_SERVER", "").strip()
MT5_PATH = os.getenv("MT5_PATH", "").strip()
DEFAULT_SYMBOL = os.getenv("TRADING_SYMBOL", "XAUUSD").strip().upper()

for key, value in {
    "base_lot": 0.01,
    "current_lot": 0.01,
    "consecutive_wins": 0,
    "trade_history": [],
    "last_analysis": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = value


def round_lot(value, step=0.01):
    step = float(step) if float(step) > 0 else 0.01
    return max(0.01, round(round(float(value) / step) * step, 2))


def parse_confidence(value):
    try:
        return max(0, min(10, int(float(str(value).replace(",", ".")))))
    except (TypeError, ValueError):
        return 0


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "oui", "vrai"}


def extract_json(text):
    if not text or not text.strip():
        raise ValueError("La réponse de Gemini est vide.")
    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", text.strip(), flags=re.I | re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Réponse JSON introuvable dans la réponse Gemini.")
    try:
        result = json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON Gemini invalide : {error}") from error
    if not isinstance(result, dict):
        raise ValueError("La réponse Gemini doit être un objet JSON.")
    return result


@st.cache_data(ttl=30, show_spinner=False)
def get_gold_price():
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/XAU", timeout=10)
        response.raise_for_status()
        rate = float(response.json().get("rates", {}).get("USD", 0))
        if rate <= 0:
            raise ValueError("Taux XAU/USD invalide.")
        return round(1 / rate, 2), None
    except requests.RequestException as error:
        return None, f"Erreur réseau : {error}"
    except (ValueError, TypeError, KeyError) as error:
        return None, f"Réponse de prix invalide : {error}"


def analyze_market(price):
    if genai is None:
        raise RuntimeError("google-generativeai n'est pas installé.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY est absente.")

    genai.configure(api_key=GEMINI_API_KEY)
    prompt = f'''Analyse XAU/USD au prix estimé de {price} USD. Réponds uniquement en JSON valide :
{{"decision":"BUY", "trend_clear":true, "confidence_score":8, "reason":"Explication courte"}}
 decision doit être BUY, SELL ou HOLD; trend_clear booléen; confidence_score entier de 1 à 10. Si insuffisant, réponds HOLD avec 1.'''
    response = genai.GenerativeModel(GEMINI_MODEL).generate_content(prompt)
    raw = extract_json(getattr(response, "text", "") or "")
    decision = str(raw.get("decision", "HOLD")).strip().upper()
    if decision not in {"BUY", "SELL", "HOLD"}:
        decision = "HOLD"
    return {
        "decision": decision,
        "trend_clear": parse_bool(raw.get("trend_clear", False)),
        "confidence_score": parse_confidence(raw.get("confidence_score", 0)),
        "reason": str(raw.get("reason", "Aucune explication fournie.")).strip(),
    }


def connect_mt5():
    if mt5 is None:
        return False, "MetaTrader5 n'est pas installé sur cet environnement."
    if not MT5_LOGIN or not MT5_PASSWORD or not MT5_SERVER:
        return False, "MT5_LOGIN, MT5_PASSWORD et MT5_SERVER sont requis."
    try:
        login = int(MT5_LOGIN)
        kwargs = {"login": login, "password": MT5_PASSWORD, "server": MT5_SERVER}
        ok = mt5.initialize(path=MT5_PATH, **kwargs) if MT5_PATH else mt5.initialize(**kwargs)
        return (True, "Connexion MT5 réussie.") if ok else (False, f"Connexion MT5 échouée : {mt5.last_error()}")
    except (ValueError, TypeError) as error:
        return False, f"Identifiants MT5 invalides : {error}"
    except Exception as error:
        return False, f"Erreur MT5 : {error}"


def send_mt5_order(action, symbol, volume, stop_loss, take_profit):
    connected, message = connect_mt5()
    if not connected:
        return False, message
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return False, f"Symbole MT5 introuvable : {symbol}"
        if not info.visible and not mt5.symbol_select(symbol, True):
            return False, f"Impossible d'activer {symbol}."
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, f"Prix MT5 indisponible pour {symbol}."
        step = float(info.volume_step or 0.01)
        volume = round_lot(max(float(info.volume_min or 0.01), min(volume, float(info.volume_max or 100))), step)
        price = float(tick.ask if action == "BUY" else tick.bid)
        digits = int(info.digits or 2)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": round(price, digits),
            "sl": round(float(stop_loss), digits),
            "tp": round(float(take_profit), digits),
            "deviation": 20,
            "magic": 20240916,
            "comment": "Bot Scalper XAU/USD",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return False, f"MT5 n'a retourné aucun résultat : {mt5.last_error()}"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Ordre refusé : {result.retcode} - {result.comment}"
        return True, f"Ordre réel exécuté. Ticket={result.order}, volume={volume}"
    except Exception as error:
        return False, f"Erreur d'exécution MT5 : {error}"
    finally:
        mt5.shutdown()


def execute_order(action, symbol, volume, price, stop_loss, take_profit, real):
    if not real:
        return True, f"Simulation : {action} {volume:.2f} lot sur {symbol} à {price:.2f}"
    return send_mt5_order(action, symbol, volume, stop_loss, take_profit)


st.title("⚡ Bot Scalper XAU/USD")
st.warning("Mode simulation par défaut. Activez le trading réel uniquement après des tests.")
price, price_error = get_gold_price()
if price_error:
    st.error(price_error)

st.sidebar.header("⚙️ Configuration")
symbol = st.sidebar.text_input("Symbole MT5", DEFAULT_SYMBOL).strip().upper()
base_lot = st.sidebar.number_input("Lot de départ", 0.01, 100.0, float(st.session_state.base_lot), 0.01)
lot_step = st.sidebar.number_input("Augmentation tous les 2 trades", 0.01, 100.0, 0.01, 0.01)
max_lot = st.sidebar.number_input("Lot maximum", 0.01, 100.0, 0.05, 0.01)
min_confidence = st.sidebar.slider("Confiance minimale", 1, 10, 8)
real_trading = st.sidebar.checkbox("⚠️ Activer le trading réel MT5", False)

st.session_state.base_lot = round_lot(base_lot)
st.session_state.current_lot = min(max(st.session_state.current_lot, st.session_state.base_lot), max_lot)
st.sidebar.write(f"📊 Lot actuel : `{st.session_state.current_lot:.2f}`")
st.sidebar.write(f"🔥 Trades consécutifs : `{st.session_state.consecutive_wins}`")
st.sidebar.error("TRADING RÉEL ACTIVÉ") if real_trading else st.sidebar.success("Mode simulation actif")

if st.sidebar.button("🔄 Réinitialiser le lot"):
    st.session_state.current_lot = st.session_state.base_lot
    st.session_state.consecutive_wins = 0
    st.rerun()

c1, c2, c3 = st.columns(3)
c1.metric("💰 Prix estimé XAU/USD", f"${price:.2f}" if price is not None else "Indisponible")
c2.metric("🎯 Lot suivant", f"{st.session_state.current_lot:.2f}")
c3.metric("📈 Progression", f"{st.session_state.consecutive_wins % 2} / 2")

st.divider()
st.subheader("🧠 Analyse Gemini et exécution")

if st.button("🚀 Lancer l'analyse du marché", type="primary"):
    if price is None:
        st.error("Prix de l'or indisponible.")
    elif not GEMINI_API_KEY:
        st.error("GEMINI_API_KEY est manquante.")
    elif max_lot < st.session_state.base_lot:
        st.error("Le lot maximum doit être supérieur au lot de départ.")
    else:
        try:
            with st.spinner("Analyse en cours..."):
                analysis = analyze_market(price)
            st.session_state.last_analysis = analysis
            decision = analysis["decision"]
            confidence = analysis["confidence_score"]
            st.write(f"**Décision :** `{decision}` | **Tendance claire :** `{analysis['trend_clear']}` | **Confiance :** `{confidence}/10`")
            st.info(f"**Analyse :** {analysis['reason']}")
            allowed = decision in {"BUY", "SELL"} and analysis["trend_clear"] and confidence >= min_confidence
            if not allowed:
                st.warning("Aucun ordre envoyé : conditions non réunies.")
                st.session_state.consecutive_wins = 0
                st.session_state.current_lot = st.session_state.base_lot
            else:
                trade_lot = round_lot(st.session_state.current_lot)
                stop_loss = round(price - 1.5 if decision == "BUY" else price + 1.5, 2)
                take_profit = round(price + 3.0 if decision == "BUY" else price - 3.0, 2)
                success, message = execute_order(decision, symbol, trade_lot, price, stop_loss, take_profit, real_trading)
                if not success:
                    st.error(message)
                else:
                    st.success(message)
                    st.session_state.consecutive_wins += 1
                    next_lot = st.session_state.current_lot
                    if st.session_state.consecutive_wins % 2 == 0:
                        next_lot = round_lot(min(st.session_state.current_lot + lot_step, max_lot))
                        st.toast(f"Deux trades validés. Prochain lot : {next_lot:.2f}", icon="📈")
                    st.session_state.current_lot = next_lot
                    st.session_state.trade_history.insert(0, {
                        "Date UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "Symbole": symbol,
                        "Prix": round(price, 2),
                        "Type": decision,
                        "Lot utilisé": trade_lot,
                        "Stop Loss": stop_loss,
                        "Take Profit": take_profit,
                        "Confiance": f"{confidence}/10",
                        "Mode": "RÉEL" if real_trading else "SIMULATION",
                        "Statut": "Exécuté",
                    })
        except Exception as error:
            st.error(f"Erreur pendant l'analyse : {error}")

if st.session_state.last_analysis:
    st.subheader("📊 Dernière analyse")
    st.json(st.session_state.last_analysis)

st.subheader("📋 Historique des ordres")
if st.session_state.trade_history:
    st.dataframe(st.session_state.trade_history, use_container_width=True, hide_index=True)
else:
    st.caption("Aucun ordre enregistré.")                confidence = data.get("confidence_score", 0)
                reason = data.get("reason", "")

                st.write(f"**Décision IA :** `{decision}` | **Tendance Claire :** `{trend_clear}` | **Confiance :** `{confidence}/10`")
                st.info(f"**Analyse :** {reason}")

                # Condition : Ordre valide + Tendance claire + Score >= 8
                if decision in ["BUY", "SELL"] and trend_clear and confidence >= 8:
                    sl = price - 1.5 if decision == "BUY" else price + 1.5
                    tp = price + 3.0 if decision == "BUY" else price - 3.0

                    # Exécution de l'ordre
                    execute_exness_order(action=decision, symbol="XAUUSD", volume=st.session_state.current_lot, sl=sl, tp=tp)
                    st.success(f"✅ Ordre **{decision}** exécuté automatiquement avec **{st.session_state.current_lot:.2f} Lot**.")

                    # Mise à jour du compteur
                    st.session_state.consecutive_wins += 1

                    # Augmentation du lot tous les 2 trades réussis
                    if st.session_state.consecutive_wins > 0 and st.session_state.consecutive_wins % 2 == 0:
                        new_lot = min(st.session_state.current_lot + lot_step, max_lot)
                        st.session_state.current_lot = round(new_lot, 2)
                        st.toast(f"🔥 2 trades validés ! Prochain lot : {st.session_state.current_lot:.2f}", icon="📈")

                    st.session_state.trade_history.insert(0, {
                        "Prix": price,
                        "Type": decision,
                        "Lot": st.session_state.current_lot,
                        "Confiance": f"{confidence}/10",
                        "Statut": "Exécuté"
                    })
                else:
                    st.warning("⚠️ Tendance incertaine ou recommandation HOLD. Pas d'augmentation du lot.")
                    # Reinitialisation du lot si la tendance n'est pas sûre
                    st.session_state.consecutive_wins = 0
                    st.session_state.current_lot = st.session_state.base_lot

            except Exception as e:
                st.error(f"Erreur d'analyse : {e}")

# --- HISTORIQUE DES POSITIONS ---
st.subheader("📋 Historique des Ordres")
if st.session_state.trade_history:
    st.table(st.session_state.trade_history)
else:
    st.caption("Aucune position enregistrée.")
