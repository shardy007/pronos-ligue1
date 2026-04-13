import sqlite3
import pandas as pd
import os
from datetime import datetime

# --- CONFIGURATION ---
FILE_NAME = "stats.xlsx"
DB_NAME = "ligue1_data_v2.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DROP TABLE IF EXISTS joueurs')
    c.execute('''
        CREATE TABLE joueurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            team_name TEXT,
            position TEXT,
            age TEXT,
            matches_played INTEGER,
            starts INTEGER,
            minutes INTEGER,
            goals INTEGER,
            assists INTEGER,
            yellow INTEGER,
            red INTEGER,
            xg FLOAT,
            xa FLOAT,
            last_updated TEXT
        )
    ''')
    conn.commit()
    conn.close()

def update_from_excel():
    if not os.path.exists(FILE_NAME):
        print(f"❌ Erreur : '{FILE_NAME}' introuvable.")
        return

    try:
        print(f"⏳ Lecture de {FILE_NAME}...")
        df = pd.read_excel(FILE_NAME)

        # 🎯 MAPPAGE : On fait correspondre tes colonnes Excel "Perf" aux noms de la base SQL
        # Vérifie bien que ces noms sont IDENTIQUES à ceux dans ton fichier Excel
        mapping = {
            'Player': 'name',
            'Squad': 'team_name',
            'Pos': 'position',
            'Age': 'age',
            'MP': 'matches_played',
            'Starts': 'starts',
            'Min': 'minutes',
            'PerfGls': 'goals',
            'PerfAst': 'assists',
            'PerfCrdY': 'yellow',
            'PerfCrdR': 'red',
            'PerfxG': 'xg',
            'PerfxAG': 'xa'
        }

        # Sécurité : on ne garde que les colonnes qui existent réellement dans l'Excel
        available_cols = [c for c in mapping.keys() if c in df.columns]
        
        if not available_cols:
            print("❌ Aucune colonne reconnue ! Voici les colonnes détectées dans ton Excel :")
            print(df.columns.tolist())
            return

        # On filtre et on renomme
        df_filtered = df[available_cols].copy()
        df_filtered.rename(columns=mapping, inplace=True)

        print("⚙️ Nettoyage et cumul des transferts...")

        # Conversion numérique (pour pouvoir faire des sommes après)
        num_cols = ['matches_played', 'starts', 'minutes', 'goals', 'assists', 'yellow', 'red', 'xg', 'xa']
        for col in num_cols:
            if col in df_filtered.columns:
                df_filtered[col] = df_filtered[col].astype(str).str.replace(',', '.')
                df_filtered[col] = pd.to_numeric(df_filtered[col], errors='coerce').fillna(0)

        # Aggrégation par joueur (additionne Marseille + Angers par exemple)
        # On utilise le dictionnaire pour ne pas faire d'erreur sur les colonnes absentes
        agg_dict = {
            'team_name': 'last', 'position': 'last', 'age': 'last'
        }
        for col in num_cols:
            if col in df_filtered.columns:
                agg_dict[col] = 'sum'

        df_final = df_filtered.groupby('name').agg(agg_dict).reset_index()

        # Insertion en base
        init_db()
        conn = sqlite3.connect(DB_NAME)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        df_final['last_updated'] = now
        
        df_final.to_sql('joueurs', conn, if_exists='append', index=False)
        conn.close()
        
        print(f"🎉 SUCCÈS ! {len(df_final)} joueurs importés depuis l'Excel.")

    except Exception as e:
        print(f"❌ Erreur lors de l'import : {e}")

if __name__ == "__main__":
    update_from_excel()