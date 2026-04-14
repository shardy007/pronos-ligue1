import streamlit as st
import requests
import pandas as pd
import sqlite3
import json
from datetime import datetime

# --- CONFIGURATION ---
API_KEY = "b231f23929014616a427bad3be898c6b" 
BASE_URL = "https://api.football-data.org/v4/"
HEADERS = {"X-Auth-Token": API_KEY}
LIGUE_1_ID = "FL1"
DB_NAME = "ligue1_data_v2.db"

# --- INITIALISATION DB ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # On stocke les endpoints avec leur date de mise à jour
    c.execute('''CREATE TABLE IF NOT EXISTS api_cache 
                 (endpoint TEXT PRIMARY KEY, data TEXT, last_updated TEXT)''')
 
    # NOUVELLE TABLE POUR LES PRONOS
    c.execute('''CREATE TABLE IF NOT EXISTS pronostics 
                 (match_id INTEGER PRIMARY KEY, 
                  home_team TEXT, 
                  away_team TEXT, 
                  pred_home INTEGER, 
                  pred_away INTEGER, 
                  real_home INTEGER, 
                  real_away INTEGER,
                  status TEXT)''')
    conn.commit()
    conn.close()

def get_data(endpoint, force_refresh=False):
    init_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT data, last_updated FROM api_cache WHERE endpoint = ?", (endpoint,))
    row = c.fetchone()
    
    # Stratégie de mise à jour : 
    # Si c'est un match en direct ou récent, on rafraîchit toutes les 15 min.
    # Si c'est une fiche club (statique), on garde 1 semaine.
    cache_duration = 900 if "matches" in endpoint else 604800 
    
    now = datetime.now()
    
    if row and not force_refresh:
        last_updated = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S')
        if (now - last_updated).total_seconds() < cache_duration:
            conn.close()
            return json.loads(row[0])

    try:
        response = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        
        c.execute("INSERT OR REPLACE INTO api_cache (endpoint, data, last_updated) VALUES (?, ?, ?)",
                  (endpoint, json.dumps(data), now.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return data
    except Exception as e:
        if row: return json.loads(row[0])
        st.error(f"Erreur API : {e}")
        return None

# --- ALGORITHME DE PRONOSTIC ---
def predict_match(home_name, away_name, standings):
    try:
        h = next(t for t in standings if t['team']['name'] == home_name)
        a = next(t for t in standings if t['team']['name'] == away_name)
        h_force = (h['points'] / h['playedGames']) + (h['goalDifference'] / 50) + 0.3
        a_force = (a['points'] / a['playedGames']) + (a['goalDifference'] / 50)
        return max(0, int(h_force * 1.3)), max(0, int(a_force * 1.1))
    except: return "?", "?"

def predict_match2(home_team_id, away_team_id, standings, all_matches, w_base=0.4, w_form=0.3, w_venue=0.3):
    """
    Algorithme de prédiction V2 avec pondérations ajustables :
    """
    try:
        # 1. Données de base du classement
        h_entry = next(t for t in standings if t['team']['id'] == home_team_id)
        a_entry = next(t for t in standings if t['team']['id'] == away_team_id)
        
        # 2. Calcul de la Forme sur les 3 derniers matchs (V=3, N=1, D=0)
        def get_form_points(team_id, matches_data):
            form_str = calculate_recent_form(team_id, matches_data, limit=3)
            pts = 0
            for res in form_str.split():
                if res == 'W': pts += 3
                elif res == 'D': pts += 1
            return pts / 9  

        h_form = get_form_points(home_team_id, all_matches)
        a_form = get_form_points(away_team_id, all_matches)

        # 3. Performance Domicile vs Extérieur
        def get_venue_score(team_id, matches_data, mode='home'):
            finished = [m for m in matches_data.get('matches', []) if m['status'] == 'FINISHED']
            relevant = [m for m in finished if m[f'{mode}Team']['id'] == team_id]
            if not relevant: return 0.5
            wins = len([m for m in relevant if m['score']['winner'] == (mode.upper() + '_TEAM')])
            return wins / len(relevant)

        h_home_perf = get_venue_score(home_team_id, all_matches, 'home')
        a_away_perf = get_venue_score(away_team_id, all_matches, 'away')

        # 4. Calcul de la force finale (Pondération DYNAMIQUE)
        h_base = (h_entry['points'] / h_entry['playedGames']) / 3
        a_base = (a_entry['points'] / a_entry['playedGames']) / 3

        # On utilise les variables w_base, w_form, w_venue ici :
        h_final = (h_base * w_base) + (h_form * w_form) + (h_home_perf * w_venue)
        a_final = (a_base * w_base) + (a_form * w_form) + (a_away_perf * w_venue)

        # Transformation en buts
        h_goals = round(h_final * 2.5 + 0.3) 
        a_goals = round(a_final * 2.5)

        return int(h_goals), int(a_goals)

    except Exception as e:
        return 1, 1


def calculate_exact_score(home_id, away_id, standings, all_matches, db_path):
    conn = sqlite3.connect(db_path)
    
    # --- 1. MOYENNES LIGUE ---
    total_goals = sum(t['goalsFor'] for t in standings)
    total_matches = sum(t['playedGames'] for t in standings)
    league_avg = (total_goals / total_matches) / 2 if total_matches > 0 else 1.3

    # --- 2. CALCUL DES 4 PILIERS ---
    
    # Pilier A : Forme Pondérée (40%)
    def get_weighted_form_score(team_id):
        # On récupère les 7 derniers résultats
        raw_form = calculate_recent_form(team_id, all_matches, limit=7)
        weights = [1.5, 1.3, 1.1, 1.0, 0.9, 0.8, 0.7] # Match le plus récent = poids 1.5
        score = 0
        for i, res in enumerate(raw_form[:len(weights)]):
            pts = 3 if res == 'W' else 1 if res == 'D' else 0
            score += pts * weights[i]
        return score / sum(weights) # Normalisé entre 0 et 3

    # Pilier B : Avantage Terrain Réel (30%)
    def get_venue_perf(team_id, mode='home'):
        m_list = [m for m in all_matches.get('matches', []) if m['status'] == 'FINISHED']
        relevant = [m for m in m_list if m[f'{mode}Team']['id'] == team_id]
        if not relevant: return 0.5
        wins = len([m for m in relevant if m['score']['winner'] == (mode.upper() + '_TEAM')])
        return wins / len(relevant)

    # Pilier C : Puissance Offensive xG (20%)
    def get_xg_boost(team_name):
        df_xg = pd.read_sql_query("SELECT SUM(xg) as total_xg FROM joueurs WHERE team_name = ?", conn, params=[team_name])
        return (df_xg['total_xg'].iloc[0] or 0) / 10 # Facteur d'ajustement

    # --- 3. CALCUL DU POTENTIEL DE BUTS (LAMBDA) ---
    h_entry = next((t for t in standings if t['team']['id'] == home_id), None)
    a_entry = next((t for t in standings if t['team']['id'] == away_id), None)

    # Base offensive (Poisson classique)
    h_base = (h_entry['goalsFor'] / h_entry['playedGames']) * (a_entry['goalsAgainst'] / a_entry['playedGames']) / league_avg
    a_base = (a_entry['goalsFor'] / a_entry['playedGames']) * (h_entry['goalsAgainst'] / h_entry['playedGames']) / league_avg

    # Application des Pilier (Multiplicateurs)
    h_boost = (get_weighted_form_score(home_id) * 0.4) + (get_venue_perf(home_id, 'home') * 0.3) + (get_xg_boost(h_entry['team']['name']) * 0.2)
    a_boost = (get_weighted_form_score(away_id) * 0.4) + (get_venue_perf(away_id, 'away') * 0.3) + (get_xg_boost(a_entry['team']['name']) * 0.2)

    conn.close()

    h_final_exp = h_base * (1 + h_boost) * 1.1 # +10% avantage domicile
    a_final_exp = a_base * (1 + a_boost)

    # CALCUL DU RISQUE (Écart entre les espérances)
    # Plus l'écart est petit, plus le match est indécis (Risque élevé)
    diff_exp = abs(h_final_exp - a_final_exp)
    if diff_exp > 1.2: 
        risk_level = "Faible"
        risk_color = "green"
    elif diff_exp > 0.6:
        risk_level = "Modéré"
        risk_color = "orange"
    else:
        risk_level = "Élevé"
        risk_color = "red"

    return round(h_final_exp), round(a_final_exp), risk_level, risk_color
	
def calculate_recent_form(team_id, all_matches_data, limit=7):
    """Calcule la forme récente en format standard (W, D, L) sur X matchs"""
    if not all_matches_data or 'matches' not in all_matches_data:
        return "N/A"
        
    past_matches = [m for m in all_matches_data['matches'] 
                    if m['status'] == 'FINISHED' 
                    and (m['homeTeam']['id'] == team_id or m['awayTeam']['id'] == team_id)]
    
    past_matches = sorted(past_matches, key=lambda x: x['utcDate'], reverse=True)
    
    form = []
    for m in past_matches[:limit]:
        is_home = m['homeTeam']['id'] == team_id
        score_h = m['score']['fullTime']['home']
        score_a = m['score']['fullTime']['away']
        
        if score_h == score_a:
            form.append("D") # Draw (Nul)
        elif (is_home and score_h > score_a) or (not is_home and score_a > score_h):
            form.append("W") # Win (Victoire)
        else:
            form.append("L") # Loss (Défaite)
            
    return " ".join(form[::-1])

def render_form_badges(form_string):
    """Transforme une chaîne de forme (W,D,L) en badges HTML colorés avec traduction V,N,D"""
    if not form_string:
        return "<span style='color:gray;'>Données non disponibles</span>"
        
    form_string = form_string.replace(',', ' ').upper()
    
    # On n'accepte QUE le format anglais (W, D, L) pour éviter toute confusion
    mapping = {
        'W': ('V', '#d4edda', '#155724'), # Win -> Victoire (Vert)
        'D': ('N', '#e2e3e5', '#383d41'), # Draw -> Nul (Gris)
        'L': ('D', '#f8d7da', '#721c24'), # Loss -> Défaite (Rouge)
    }
    
    html = '<div style="display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 15px;">'
    for char in form_string.split():
        if char in mapping:
            letter, bg_color, text_color = mapping[char]
            html += f'''
            <div style="background-color: {bg_color}; color: {text_color}; 
                        width: 32px; height: 32px; display: flex; 
                        align-items: center; justify-content: center; 
                        border-radius: 4px; font-weight: bold; font-family: Arial, sans-serif;
                        box-shadow: 0 1px 2px rgba(0,0,0,0.1);">
                {letter}
            </div>'''
    html += '</div>'
    return html

def check_performance():
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM pronostics WHERE status = 'FINISHED'", conn)
    conn.close()

    if df.empty:
        return "Pas encore de matchs terminés pour analyser."

    # Analyse du Résultat (1N2)
    def is_result_correct(row):
        pred_res = "H" if row['pred_home'] > row['pred_away'] else "A" if row['pred_away'] > row['pred_home'] else "D"
        real_res = "H" if row['real_home'] > row['real_away'] else "A" if row['real_away'] > row['real_home'] else "D"
        return pred_res == real_res

    # Analyse du Score Exact
    def is_score_exact(row):
        return row['pred_home'] == row['real_home'] and row['pred_away'] == row['real_away']

    df['Resultat_OK'] = df.apply(is_result_correct, axis=1)
    df['Score_Exact_OK'] = df.apply(is_score_exact, axis=1)

    success_rate = (df['Resultat_OK'].sum() / len(df)) * 100
    exact_rate = (df['Score_Exact_OK'].sum() / len(df)) * 100

    return success_rate, exact_rate, df

def optimize_weights(standings, all_matches):
    import numpy as np
    
    finished = [m for m in all_matches['matches'] if m['status'] == 'FINISHED']
    if not finished: return None
    
    # 1. On extrait les fonctions de calcul (pour aller plus vite)
    def get_form_points(team_id):
        form_str = calculate_recent_form(team_id, all_matches, limit=3)
        return sum(3 if r == 'W' else 1 if r == 'D' else 0 for r in form_str.split()) / 9

    def get_venue_score(team_id, mode='home'):
        relevant = [m for m in finished if m[f'{mode}Team']['id'] == team_id]
        if not relevant: return 0.5
        return len([m for m in relevant if m['score']['winner'] == (mode.upper() + '_TEAM')]) / len(relevant)

    # 2. Pré-calcul pour éviter de refaire la même chose 1000 fois
    match_data = []
    for m in finished:
        h_id, a_id = m['homeTeam']['id'], m['awayTeam']['id']
        rh, ra = m['score']['fullTime']['home'], m['score']['fullTime']['away']
        
        h_ent = next((t for t in standings if t['team']['id'] == h_id), None)
        a_ent = next((t for t in standings if t['team']['id'] == a_id), None)
        if not h_ent or not a_ent: continue
        
        match_data.append({
            'real_win': "H" if rh > ra else "A" if ra > rh else "D",
            'rh': rh, 'ra': ra,
            'h_b': (h_ent['points'] / h_ent['playedGames']) / 3,
            'a_b': (a_ent['points'] / a_ent['playedGames']) / 3,
            'h_f': get_form_points(h_id),
            'a_f': get_form_points(a_id),
            'h_v': get_venue_score(h_id, 'home'),
            'a_v': get_venue_score(a_id, 'away')
        })

    # 3. Test de toutes les combinaisons (Grid Search)
    best_res_rate, best_exact_rate = 0, 0
    best_w_res, best_w_exact = (0,0,0), (0,0,0)

    # On teste avec des pas de 5% (0.05)
    for w_base in np.arange(0.0, 1.05, 0.05):
        for w_form in np.arange(0.0, 1.05 - w_base + 0.01, 0.05):
            w_venue = round(1.0 - w_base - w_form, 2)
            if w_venue < 0: continue

            c_res, c_ex = 0, 0
            for md in match_data:
                h_final = (md['h_b'] * w_base) + (md['h_f'] * w_form) + (md['h_v'] * w_venue)
                a_final = (md['a_b'] * w_base) + (md['a_f'] * w_form) + (md['a_v'] * w_venue)

                ph2 = round(h_final * 2.5 + 0.3)
                pa2 = round(a_final * 2.5)
                p_win = "H" if ph2 > pa2 else "A" if pa2 > ph2 else "D"

                if p_win == md['real_win']: c_res += 1
                if ph2 == md['rh'] and pa2 == md['ra']: c_ex += 1

            # Sauvegarde des meilleurs
            if c_res > best_res_rate:
                best_res_rate = c_res
                best_w_res = (w_base, w_form, w_venue)
            if c_ex > best_exact_rate:
                best_exact_rate = c_ex
                best_w_exact = (w_base, w_form, w_venue)

    total = len(match_data)
    return best_w_res, (best_res_rate/total)*100, best_w_exact, (best_exact_rate/total)*100	

# --- INTERFACE ---
st.set_page_config(page_title="Ligue 1 Data Pro", layout="wide")

# Menu latéral
st.sidebar.title("🏆 Ligue 1 Pro")
pages = ["📊 Classement", "🎯 Buteurs", "🏃 Joueurs", "📅 Saison 2025-2026"]
if 'page' not in st.session_state: st.session_state.page = pages[0]

for p in pages:
    if st.sidebar.button(p): st.session_state.page = p

# Récupération du classement pour usage général
standings_data = get_data(f"competitions/{LIGUE_1_ID}/standings")
if standings_data and 'standings' in standings_data:
    standings = standings_data['standings'][0]['table']
else:
    standings = []
    st.warning("⚠️ Impossible de charger le classement (Quota API atteint ou erreur réseau).")

# PAGE : CLASSEMENT
if st.session_state.page == "📊 Classement":
    col_class, col_fiche = st.columns([2.3, 1])
    
    # On charge les matchs pour la forme
    all_season_matches = get_data(f"competitions/{LIGUE_1_ID}/matches?season=2025")
    
    with col_class:
        st.header("Classement Ligue 1")
        h = st.columns([0.8, 0.8, 4, 1, 0.8, 0.8, 0.8, 1, 1, 1, 1.2])
        h[0].write("**№**"); h[2].write("**Équipe**"); h[3].write("**MJ**");h[4].write("**V**");h[5].write("**N**");h[6].write("**D**"); h[7].write("**BP**"); h[8].write("**BC**");h[9].write("**Diff**"); h[10].write("**Pts**")
        st.divider()

        for entry in standings:
            c = st.columns([0.8, 0.8, 4, 1, 0.8, 0.8, 0.8, 1, 1, 1, 1.2])
            c[0].write(f"{entry['position']}")
            c[1].image(entry['team']['crest'], width=25)
            if c[2].button(entry['team']['name'], key=f"btn_{entry['team']['id']}"):
                st.session_state.selected_club = entry['team']['id']
            c[3].write(str(entry['playedGames']))
            c[4].caption(str(entry['won'])); c[5].caption(str(entry['draw'])); c[6].caption(str(entry['lost']))
            c[7].write(str(entry['goalsFor'])); c[8].write(str(entry['goalsAgainst']))
            
            diff = entry['goalDifference']
            color = "green" if diff > 0 else "red" if diff < 0 else "gray"
            c[9].markdown(f":{color}[{diff}]")
            c[10].write(f"**{entry['points']}**")

    with col_fiche:
        st.header("🔍 Fiche club")

        if st.session_state.get('selected_club'):
            club = get_data(f"teams/{st.session_state.selected_club}")
            if club:
                st.image(club['crest'], width=80)
                
                tla_text = f"({club['tla']})" if club.get('tla') else ""
                st.subheader(f"{club['name']} {tla_text}")
                
                st.info(f"🏟️ **Stade:** {club.get('venue', 'N/A')}")
                st.warning(f"📅 **Fondation:** {club.get('founded', 'N/A')} | 🎨 **Couleurs:** {club.get('clubColors', 'N/A')}")
                
                if club.get('coach') and club['coach'].get('name'): 
                    st.success(f"👔 **Entraîneur:** {club['coach']['name']}")
				
                    # --- CALCUL ET AFFICHAGE DES POINTS PAR MATCH ---
                    team_entry = next((t for t in standings if t['team']['id'] == club['id']), None)
                    if team_entry:
                        pts = team_entry['points']
                        mj = team_entry['playedGames']
                        ratio = pts / mj if mj > 0 else 0
                    
                        # Affichage sous forme de métrique pour que ce soit bien visible
                        st.error(f"📈 **Performance comptable:** {ratio:.2f} Pts / Match ({pts} pts en {mj} matchs)")
					
					# --- CALCUL MANUEL DOM/EXT VIA LES MATCHS ---
                    if all_season_matches and st.session_state.get('selected_club'):
                        st.divider()
                        
                        team_id = st.session_state.selected_club
                        finished_matches = [
                            m for m in all_season_matches.get('matches', []) 
                            if m['status'] == 'FINISHED' and (m['homeTeam']['id'] == team_id or m['awayTeam']['id'] == team_id)
                        ]

                        # Initialisation avec les buts marqués (gs) et encaissés (gc)
                        stats = {
                            'home': {'w': 0, 'd': 0, 'l': 0, 'total': 0, 'gs': 0, 'gc': 0},
                            'away': {'w': 0, 'd': 0, 'l': 0, 'total': 0, 'gs': 0, 'gc': 0}
                        }

                        for m in finished_matches:
                            winner = m['score']['winner']
                            gh = m['score']['fullTime']['home']
                            ga = m['score']['fullTime']['away']
                            
                            if m['homeTeam']['id'] == team_id:
                                stats['home']['total'] += 1
                                stats['home']['gs'] += gh # Buts marqués à domicile
                                stats['home']['gc'] += ga # Buts encaissés à domicile
                                if winner == 'HOME_TEAM': stats['home']['w'] += 1
                                elif winner == 'DRAW': stats['home']['d'] += 1
                                else: stats['home']['l'] += 1
                            else:
                                stats['away']['total'] += 1
                                stats['away']['gs'] += ga # Buts marqués à l'extérieur
                                stats['away']['gc'] += gh # Buts encaissés à l'extérieur
                                if winner == 'AWAY_TEAM': stats['away']['w'] += 1
                                elif winner == 'DRAW': stats['away']['d'] += 1
                                else: stats['away']['l'] += 1

                        def get_p(v, t): return (v / t) if t > 0 else 0

                        # --- AFFICHAGE CÔTE À CÔTE ---
                        col_dom, col_ext = st.columns(2)

                        with col_dom:
                            h = stats['home']
                            h_w_pct = get_p(h['w'], h['total'])
                            h_pts = (h['w'] * 3) + h['d'] # Calcul des points à domicile
                            h_ratio = h_pts / h['total'] if h['total'] > 0 else 0
                            st.write(f"🏠 **À Domicile** ({h['total']} matchs)")
                            st.progress(h_w_pct, text=f"Victoires : {h_w_pct*100:.0f}%")
                            st.caption(f"Nuls : {get_p(h['d'], h['total'])*100:.0f}% | Défaites : {get_p(h['l'], h['total'])*100:.0f}%")
                            st.caption(f"⚽ **Buts:** {h['gs']} marqués / {h['gc']} encaissés")
                            st.caption(f"📈 **Points:** {h_pts} pts ({h_ratio:.2f} Pts / Match)")

                        with col_ext:
                            a = stats['away']
                            a_w_pct = get_p(a['w'], a['total'])
                            a_pts = (a['w'] * 3) + a['d'] # Calcul des points à l'extérieur
                            a_ratio = a_pts / a['total'] if a['total'] > 0 else 0
                            st.write(f"🚀 **À l'Extérieur** ({a['total']} matchs)")
                            st.progress(a_w_pct, text=f"Victoires : {a_w_pct*100:.0f}%")
                            st.caption(f"Nuls : {get_p(a['d'], a['total'])*100:.0f}% | Défaites : {get_p(a['l'], a['total'])*100:.0f}%")
                            st.caption(f"⚽ **Buts:** {a['gs']} marqués / {a['gc']} encaissés")
                            st.caption(f"📈 **Points:** {a_pts} pts ({a_ratio:.2f} Pts / Match)")
                        
                        st.divider()

                    # Forme récente
                    forme_7 = calculate_recent_form(st.session_state.selected_club, all_season_matches, limit=7)

                    # Forme récente
                    forme_7 = calculate_recent_form(st.session_state.selected_club, all_season_matches, limit=7)
                    st.write("**Forme (7 derniers matchs) :**")
                    st.markdown(render_form_badges(forme_7), unsafe_allow_html=True)

                with st.expander("👥 Voir l'effectif détaillé", expanded=False):
                    squad_data = []
                    for p in club.get('squad', []):
                        age = "N/A"
                        if p.get('dateOfBirth'):
                            try:
                                dob = datetime.strptime(p['dateOfBirth'], '%Y-%m-%d')
                                age = (datetime.now() - dob).days // 365
                            except: pass
                        squad_data.append({
                            "N°": p.get('shirtNumber', '-'), "Nom": p['name'], "Poste": p.get('position', 'N/A'), "Âge": age
                        })
                    st.dataframe(pd.DataFrame(squad_data), hide_index=True, use_container_width=True)
        else:
            st.write("Cliquer sur un nom d'équipe dans le classement pour voir les détails.")
			
# PAGE : SAISON 2025-2026 (RÉSULTATS + PRONOS)
elif st.session_state.page == "📅 Saison 2025-2026":
    st.header("📅 Saison 2025-2026 & Pronostics")
    
    # --- 1. CHARGEMENT DES DONNÉES ---
    all_matches = get_data(f"competitions/{LIGUE_1_ID}/matches?season=2025")
    conn = sqlite3.connect(DB_NAME)
    
    # --- 2. LOGIQUE DU SLIDER ---
    # 1. On tente de récupérer la journée actuelle officielle via l'API
    api_current_day = all_matches.get('competition', {}).get('currentSeason', {}).get('currentMatchday')
    
    # 2. Si l'API ne le donne pas, on cherche la première journée non terminée (ton ancienne logique)
    if not api_current_day:
        scheduled = [m['matchday'] for m in all_matches['matches'] if m['status'] != 'FINISHED']
        api_current_day = min(scheduled) if scheduled else 34

    # 3. On affiche le slider avec api_current_day comme valeur par défaut
    day_choice = st.slider("Choisir une Journée", 1, 34, int(api_current_day))

    # --- RÉGLAGES EN DIRECT DE LA V2 (Sidebar) ---
    st.sidebar.markdown("---")
    st.sidebar.subheader("⚙️ Labo Modèle V2")
    
    poids_class = st.sidebar.slider("Poids du Classement", 0.0, 1.0, 0.40, 0.05)
    poids_forme = st.sidebar.slider("Poids de la Forme", 0.0, 1.0, 0.30, 0.05)
    poids_lieu = st.sidebar.slider("Poids Domicile/Ext", 0.0, 1.0, 0.30, 0.05)

    st.sidebar.markdown("---")
    
    # BOUTON D'OPTIMISATION AUTOMATIQUE
    if st.sidebar.button("🚀 Auto-Optimisation V2"):
        with st.spinner("L'IA teste toutes les combinaisons..."):
            best_res, pct_res, best_ex, pct_ex = optimize_weights(standings, all_matches)
            
            st.sidebar.success("✅ Calcul terminé !")
            
            st.sidebar.markdown(f"**🏆 Meilleur pour le Résultat ({pct_res:.1f}%)**")
            st.sidebar.code(f"Classement: {best_res[0]*100:.0f}%\nForme: {best_res[1]*100:.0f}%\nDom/Ext: {best_res[2]*100:.0f}%")
            
            st.sidebar.markdown(f"**🎯 Meilleur pour Score Exact ({pct_ex:.1f}%)**")
            st.sidebar.code(f"Classement: {best_ex[0]*100:.0f}%\nForme: {best_ex[1]*100:.0f}%\nDom/Ext: {best_ex[2]*100:.0f}%")

    # Normalisation pour que le total fasse toujours 100%
    total_poids = poids_class + poids_forme + poids_lieu
    if total_poids > 0:
        poids_class, poids_forme, poids_lieu = poids_class/total_poids, poids_forme/total_poids, poids_lieu/total_poids

    # --- 3. CALCUL DES STATISTIQUES DOUBLES (V1 vs V2) ---
    c = conn.cursor()
    c.execute("SELECT match_id, pred_home, pred_away FROM pronostics")
    db_preds = {row[0]: (row[1], row[2]) for row in c.fetchall()}

    v1_res_g = v1_ex_g = v2_res_g = v2_ex_g = count_g = 0
    v1_res_d = v1_ex_d = v2_res_d = v2_ex_d = count_d = 0

    for m in all_matches['matches']:
        if m['status'] == 'FINISHED':
            rh, ra = m['score']['fullTime']['home'], m['score']['fullTime']['away']
            real_win = "H" if rh > ra else "A" if ra > rh else "D"
            
            # Récupération/Calcul V1
            if m['id'] in db_preds:
                ph1, pa1 = db_preds[m['id']]
            else:
                ph1, pa1 = predict_match(m['homeTeam']['name'], m['awayTeam']['name'], standings)
            pred_win1 = "H" if ph1 > pa1 else "A" if pa1 > ph1 else "D"
            
            # Calcul V2 dynamique AVEC LES CURSEURS
            ph2, pa2 = predict_match2(m['homeTeam']['id'], m['awayTeam']['id'], standings, all_matches, poids_class, poids_forme, poids_lieu)
            pred_win2 = "H" if ph2 > pa2 else "A" if pa2 > ph2 else "D"

            # Incrémentation Global
            count_g += 1
            if pred_win1 == real_win: v1_res_g += 1
            if ph1 == rh and pa1 == ra: v1_ex_g += 1
            if pred_win2 == real_win: v2_res_g += 1
            if ph2 == rh and pa2 == ra: v2_ex_g += 1

            # Incrémentation Journée
            if m['matchday'] == day_choice:
                count_d += 1
                if pred_win1 == real_win: v1_res_d += 1
                if ph1 == rh and pa1 == ra: v1_ex_d += 1
                if pred_win2 == real_win: v2_res_d += 1
                if ph2 == rh and pa2 == ra: v2_ex_d += 1

    def calc_pct(val, total):
        return (val / total * 100) if total > 0 else 0
	
    # --- 4. AFFICHAGE DES ENCARTS DE PERFORMANCE ---
    col_g, col_d = st.columns(2)
    
    with col_g:
        st.markdown(f"### 🌍 Global ({count_g} matchs)")
        c1, c2 = st.columns(2)
        c1.metric("✅ Résultat V1", f"{calc_pct(v1_res_g, count_g):.1f}%")
        c2.metric("🎯 Exact V1", f"{calc_pct(v1_ex_g, count_g):.1f}%")
        
        c3, c4 = st.columns(2)
        c3.metric("✅ Résultat V2", f"{calc_pct(v2_res_g, count_g):.1f}%")
        c4.metric("🎯 Exact V2", f"{calc_pct(v2_ex_g, count_g):.1f}%")

    with col_d:
        st.markdown(f"### 📍 Journée {day_choice} ({count_d} matchs)")
        if count_d > 0:
            c5, c6 = st.columns(2)
            c5.metric("✅ Résultat V1", f"{calc_pct(v1_res_d, count_d):.1f}%")
            c6.metric("🎯 Exact V1", f"{calc_pct(v1_ex_d, count_d):.1f}%")
            
            c7, c8 = st.columns(2)
            c7.metric("✅ Résultat V2", f"{calc_pct(v2_res_d, count_d):.1f}%")
            c8.metric("🎯 Exact V2", f"{calc_pct(v2_ex_d, count_d):.1f}%")
        else:
            st.info("Matchs de la journée non terminés.")

    st.divider()

    # --- 5. AFFICHAGE DU TABLEAU DÉTAILLÉ ---
    matches_to_show = [m for m in all_matches['matches'] if m['matchday'] == day_choice]

    # NOUVEAU RATIO (8 colonnes pour intégrer V1, stats V1, V2, stats V2 et Réel)
    col_ratios = [2.2, 1.2, 0.7, 0.8, 1.2, 0.7, 0.8, 1.0]
    
    # ENTÊTE DES COLONNES
    h_col = st.columns(col_ratios)
    h_col[0].write("**Match**")
    h_col[1].write("**Prono V1**")
    h_col[2].write("**Rés. V1**")
    h_col[3].write("**Exact V1**")
    h_col[4].write("**Prono V2**")
    h_col[5].write("**Rés. V2**")
    h_col[6].write("**Exact V2**")
    h_col[7].write("**Réel**")
    st.markdown("---")

    for m in matches_to_show:
        h_name = m['homeTeam']['name']
        a_name = m['awayTeam']['name']
        h_id, a_id = m['homeTeam']['id'], m['awayTeam']['id']
        m_id = m['id']
        
        # 1. Gestion du Pronostic V1 (Base de données)
        c.execute("SELECT pred_home, pred_away FROM pronostics WHERE match_id = ?", (m_id,))
        row = c.fetchone()
        
        if row:
            ph, pa = row[0], row[1]
        else:
            ph, pa = predict_match(h_name, a_name, standings)
            conn.execute('''INSERT OR IGNORE INTO pronostics (match_id, home_team, away_team, pred_home, pred_away, status) 
                            VALUES (?, ?, ?, ?, ?, ?)''', (m_id, h_name, a_name, ph, pa, m['status']))
            conn.commit()

        # 2. Calcul du Pronostic V2 (Dynamique)
        ph2, pa2 = predict_match2(h_id, a_id, standings, all_matches, poids_class, poids_forme, poids_lieu)

        # 3. Analyse des résultats
        real_score = "-"
        res_icon1 = exact_icon1 = res_icon2 = exact_icon2 = "⏳"
        
        if m['status'] == 'FINISHED':
            rh = m['score']['fullTime']['home']
            ra = m['score']['fullTime']['away']
            real_score = f"{rh} - {ra}"
            
            conn.execute("UPDATE pronostics SET real_home = ?, real_away = ?, status = 'FINISHED' WHERE match_id = ?", (rh, ra, m_id))
            conn.commit()

            real_win = "H" if rh > ra else "A" if ra > rh else "D"
            
            # Validation V1
            pred_win1 = "H" if ph > pa else "A" if pa > ph else "D"
            res_icon1 = "✅" if pred_win1 == real_win else "❌"
            exact_icon1 = "🎯" if (ph == rh and pa == ra) else "❌"
            
            # Validation V2
            pred_win2 = "H" if ph2 > pa2 else "A" if pa2 > ph2 else "D"
            res_icon2 = "✅" if pred_win2 == real_win else "❌"
            exact_icon2 = "🎯" if (ph2 == rh and pa2 == ra) else "❌"

        # 4. AFFICHAGE DE LA LIGNE
        row_col = st.columns(col_ratios)
        row_col[0].write(f"{h_name} - {a_name}")
        row_col[1].info(f"{ph} - {pa}") 
        row_col[2].write(f"<center>{res_icon1}</center>", unsafe_allow_html=True)
        row_col[3].write(f"<center>{exact_icon1}</center>", unsafe_allow_html=True)
        row_col[4].warning(f"{ph2} - {pa2}") 
        row_col[5].write(f"<center>{res_icon2}</center>", unsafe_allow_html=True)
        row_col[6].write(f"<center>{exact_icon2}</center>", unsafe_allow_html=True)
        row_col[7].write(f"**{real_score}**")
        
    conn.close()

# PAGE : JOUEURS (VERSION FBRef EXCEL)
elif st.session_state.page == "🏃 Joueurs":
    st.header("🏃 Base de données des joueurs (Saison 2025-2026)")
    st.caption("Données issues de l'export FBRef (Excel)")

    try:
        conn = sqlite3.connect(DB_NAME)
        
        # 1. Barre latérale de filtres ou colonnes de filtres en haut
        col_f1, col_f2, col_f3 = st.columns([2, 2, 1])
        
        with col_f1:
            # Récupération de la liste des clubs
            clubs_df = pd.read_sql_query("SELECT DISTINCT team_name FROM joueurs ORDER BY team_name", conn)
            selected_team = st.selectbox("🔍 Filtrer par club :", ["Tous les clubs"] + clubs_df['team_name'].tolist())
        
        with col_f2:
            search_name = st.text_input("👤 Chercher un joueur :", placeholder="Ex: Mbappé...")
            
        with col_f3:
            min_minutes = st.number_input("⏱️ Min. Minutes", value=0, step=90)

        # 2. Construction de la requête SQL
        query = "SELECT name, team_name, position, age, matches_played, starts, minutes, goals, assists, xg, xa FROM joueurs WHERE minutes >= ?"
        params = [min_minutes]
        
        if selected_team != "Tous les clubs":
            query += " AND team_name = ?"
            params.append(selected_team)
        if search_name:
            query += " AND name LIKE ?"
            params.append(f"%{search_name}%")
        
        query += " ORDER BY goals DESC, xg DESC" # Tri par défaut : les meilleurs buteurs
        
        df_joueurs = pd.read_sql_query(query, conn, params=params)
        conn.close()

        if not df_joueurs.empty:
            # Traduction des postes FBRef (FW=Attaquant, MF=Milieu, DF=Défenseur, GK=Gardien)
            mapping_postes = {"FW": "Attaquant", "MF": "Milieu", "DF": "Défenseur", "GK": "Gardien"}
            # FBRef combine parfois les postes (ex: FW,MF), on simplifie pour l'affichage
            df_joueurs['position'] = df_joueurs['position'].apply(lambda x: mapping_postes.get(str(x).split(',')[0], x))

            # Renommage des colonnes pour l'utilisateur
            df_joueurs.columns = ['Nom', 'Club', 'Poste', 'Âge', 'Matchs', 'Titulaire', 'Minutes', 'Buts', 'Passes', 'xG', 'xA']

            st.write(f"📊 **{len(df_joueurs)}** joueurs correspondent à vos critères.")

            # 3. Affichage Pro avec configuration des colonnes
            st.dataframe(
                df_joueurs,
                column_config={
                    "Nom": st.column_config.TextColumn("Nom", width="medium"),
                    "Club": st.column_config.TextColumn("Club"),
                    "Matchs": st.column_config.NumberColumn("MP", help="Matchs Joués"),
                    "Titulaire": st.column_config.NumberColumn("Tit.", help="Titularisations"),
                    "Minutes": st.column_config.ProgressColumn("Temps de jeu", format="%d min", min_value=0, max_value=int(df_joueurs['Minutes'].max() or 1)),
                    "Buts": st.column_config.NumberColumn("⚽ Buts"),
                    "Passes": st.column_config.NumberColumn("👟 Passes"),
                    "xG": st.column_config.NumberColumn("🔥 xG", format="%.2f", help="Expected Goals : dangerosité du joueur"),
                    "xA": st.column_config.NumberColumn("🎯 xA", format="%.2f", help="Expected Assists : qualité de passe")
                },
                use_container_width=True,
                hide_index=True,
                height=600
            )
            
            # Petit résumé statistique
            c1, c2, c3 = st.columns(3)
            c1.metric("Meilleur Buteur", df_joueurs.iloc[0]['Nom'], f"{df_joueurs.iloc[0]['Buts']} buts")
            # Tri par xG pour trouver le joueur le plus dangereux
            top_xg = df_joueurs.sort_values(by='xG', ascending=False).iloc[0]
            c2.metric("Plus Dangereux (xG)", top_xg['Nom'], f"{top_xg['xG']:.2f} xG")
            # Moyenne d'âge
            avg_age = df_joueurs['Âge'].str.split('-').str[0].astype(float).mean()
            c3.metric("Âge Moyen", f"{avg_age:.1f} ans")

        else:
            st.warning("Aucune donnée trouvée. Vérifiez vos filtres ou lancez 'update_db.py'.")
            
    except Exception as e:
        st.error(f"Erreur d'affichage : {e}")

# PAGE : BUTEURS
elif st.session_state.page == "🎯 Buteurs":
    st.header("🎯 Classement des buteurs")
    
    scorers_data = get_data(f"competitions/{LIGUE_1_ID}/scorers")
    
    if scorers_data and 'scorers' in scorers_data:
        all_scorers = []
        
        for i, s in enumerate(scorers_data['scorers']):
            goals = s.get('goals', 0)
            played = s.get('playedMatches', 0)
            assists = s.get('assists', 0) or 0
            penalties = s.get('penalties', 0) or 0
            
            ratio = round(goals / played, 2) if played > 0 else 0
            
            all_scorers.append({
                "Rang": i + 1,
                "Joueur": s['player']['name'],
                "Nationalité": s['player'].get('nationality', 'N/A'),
                "Club": s['team']['name'],
                "MJ": played,
                "Buts ⚽": goals,
                "Passes D. 👟": assists,
                "Pénos": penalties,
                "Ratio B/M": ratio,
                "Total (B+P)": goals + assists
            })

        df_scorers = pd.DataFrame(all_scorers)

        # Podium visuel
        top_cols = st.columns(3)
        for i in range(min(3, len(all_scorers))):
            player = all_scorers[i]
            with top_cols[i]:
                st.metric(
                    label=f"#{i+1} {player['Joueur']}", 
                    value=f"{player['Buts ⚽']} Buts", 
                    delta=f"{player['MJ']} matchs"
                )
                st.caption(f"🏃 {player['Club']}")

        st.divider()

        # Tableau complet
        st.dataframe(
            df_scorers,
            column_config={
                "Buts ⚽": st.column_config.ProgressColumn(
                    "Buts",
                    help="Nombre de buts marqués",
                    format="%d",
                    min_value=0,
                    max_value=int(df_scorers["Buts ⚽"].max()) if not df_scorers.empty else 20,
                ),
                "Passes D. 👟": st.column_config.NumberColumn("Passes D.", format="%d"),
                "Ratio B/M": st.column_config.NumberColumn("Ratio", format="%.2f ⚡"),
                "Total (B+P)": st.column_config.NumberColumn("Total", format="%d 🏆"),
                "Pénos": st.column_config.NumberColumn("Pén.", format="%d"),
                "MJ": st.column_config.NumberColumn("MJ")
            },
            use_container_width=True,
            hide_index=True
        )

        # Sidebar Stats
        st.sidebar.markdown("---")
        st.sidebar.subheader("📊 Analyse du Top")
        st.sidebar.metric("Moyenne Buts", f"{df_scorers['Buts ⚽'].mean():.1f}")
        st.sidebar.metric("Meilleur Passeur", f"{df_scorers['Passes D. 👟'].max()}")
        
    else:
        st.error("Impossible de charger les statistiques des buteurs.")