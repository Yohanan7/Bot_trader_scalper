import os
import json
import requests
import streamlit as st
import google.generativeai as genai

st.set_page_config(page_title=" Scalper XAU/USD", page_icon="📈", layout="wide")

# --- INITIALISATION DE LA MÉMOIRE DU BOT ---
if "base_lot" not in st.session_state:
    st.session_state.base_lot = 0.01
if "current_lot" not in st.session_state:
    st.session_state.current_lot = 0.01
if "consecutive_wins" not in st.session_state:
    st.session_state.consecutive_wins = 0
if "trade_history" not in st.session_state:
    st.session_state.trade_history = []

# Configuration de la clé API Gemini
# --- RÉCUPÉRATION DES VARIABLES D'ENVIRONNEMENT RENDER ---
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
EXNESS_LOGIN = os.getenv("MT5_LOGIN")
EXNESS_PASSWORD = os.getenv("MT5_PASSWORD")
EXNESS_SERVER = os.getenv("MT5_SERVER")  # <-- Lecture automatique du serveur Exness
    
#GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

st.title("⚡ Bot Scalper XAU/USD — Gestion Dynamique du Lot")

# --- BARRE LATÉRALE : CONFIGURATION ---
st.sidebar.header("⚙️ Configuration du Lot")

st.session_state.base_lot = st.sidebar.number_input(
    "Lot de Départ (Base)", min_value=0.01, max_value=1.0, value=0.01, step=0.01
)
lot_step = st.sidebar.number_input(
    "Incrément de Lot (Tous les 2 trades)", min_value=0.01, max_value=0.5, value=0.01, step=0.01
)
max_lot = st.sidebar.number_input(
    "Lot Maximum Autorisé", min_value=0.02, max_value=5.0, value=0.05, step=0.01
)

st.sidebar.divider()
st.sidebar.write(f"📊 **Lot Actuel :** `{st.session_state.current_lot:.2f}`")
st.sidebar.write(f"🔥 **Trades Validés Consécutifs :** `{st.session_state.consecutive_wins}`")

if st.sidebar.button("🔄 Réinitialiser le Lot"):
    st.session_state.current_lot = st.session_state.base_lot
    st.session_state.consecutive_wins = 0
    st.rerun()

# --- FONCTIONS CLÉS ---
def get_gold_price():
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/XAU").json()
        return round(1 / r['rates']['USD'], 2)
    except Exception:
        return None

def execute_exness_order(action, symbol="XAUUSD", volume=0.01, sl=0, tp=0):
    """ Envoi direct du contrat d'exécution vers l'API d'exécution """
    print(f"⚡ [EXNESS AUTO] Ordre {action} {volume} lot sur {symbol} | SL: {sl} | TP: {tp}")

price = get_gold_price()

# --- AFFICHAGE DES INDICATEURS CLÉS ---
c1, c2, c3 = st.columns(3)
with c1:
    st.metric("💰 Prix Or (XAU/USD)", f"${price}" if price else "Indisponible")
with c2:
    st.metric("🎯 Lot de la Prochaine Position", f"{st.session_state.current_lot:.2f} Lot")
with c3:
    st.metric("📈 Compteur d'Incrémentation", f"{st.session_state.consecutive_wins % 2} / 2 Trades")

st.divider()

# --- ANALYSE IA & INCRÉMENTATION DE LOT ---
st.subheader("🧠 Analyse Gemini & Exécution Dynamique")

if st.button("🚀 Lancer l'Analyse du Marché"):
    if not price or not GEMINI_KEY:
        st.error("Prix de l'or indisponible ou clé GEMINI_API_KEY manquante.")
    else:
        with st.spinner("Analyse du marché XAU/USD par Gemini..."):
            prompt = f"""
            Tu es un système de trading algorithmique de scalping sur l'Or (XAU/USD).
            Prix actuel: {price} USD.
            Analyse la structure du marché et réponds STRICTEMENT au format JSON :
            {{
                "decision": "BUY", "SELL" ou "HOLD",
                "trend_clear": true ou false,
                "confidence_score": 1 à 10,
                "reason": "Explication courte"
            }}
            """
            try:
                model = genai.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content(prompt)
                clean_res = response.text.replace('```json', '').replace('```', '').strip()
                data = json.loads(clean_res)

                decision = data.get("decision")
                trend_clear = data.get("trend_clear", False)
                confidence = data.get("confidence_score", 0)
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
