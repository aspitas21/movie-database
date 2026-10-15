import asyncio
import json
import re
import urllib.request
from datetime import datetime
from playwright.async_api import async_playwright

# Connexion directe à ton site CinéVault (Supabase)
SU = "https://dnzrpvyeiyxplrcomsuo.supabase.co"
SK = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRuenJwdnllaXl4cGxyY29tc3VvIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTUzNjA1MCwiZXhwIjoyMDk1MTEyMDUwfQ.IZUKY4IsWIkk2LDMQTdUORtW3PhdlrlmTsu9Mng7oLA"

FICHIER_RAPPORT = "dead_links.json"
CONCURRENCE_MAX = 5
DOMAINES_CIBLES = ["gofile.io", "transfer.it", "drive.google.com", "docs.google.com"]

MOTS_CLES_MORT = {
    "gofile.io": [
        "this content does not exist", "content not found",
        "this folder is empty", "has been automatically removed", "file not found"
    ],
    "transfer.it": [
        "expired", "expiré", "not found", "introuvable",
        "no longer available", "n'est plus disponible", "deleted", "supprimé",
        "doesn't exist", "does not exist", "n'existe pas", "invalid", "invalide",
        "unavailable", "indisponible", "oops", "oups", "error", "erreur",
        "nothing here", "empty", "removed"
    ],
    "google": [
        "sorry, the file you have requested does not exist",
        "le fichier que vous avez demandé n'existe pas",
        "file is in owner's trash", "ce fichier a été placé dans la corbeille",
        "we're sorry. you can't access this item", "vous ne pouvez pas accéder à cet élément"
    ]
}

def recuperer_films_supabase():
    films = []
    offset = 0
    while True:
        url = f"{SU}/rest/v1/movies?select=*&order=created_at.asc&limit=1000&offset={offset}"
        req = urllib.request.Request(url, headers={
            "apikey": SK,
            "Authorization": f"Bearer {SK}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req) as resp:
            page = json.loads(resp.read().decode("utf-8"))
            for row in page:
                d = row.get("data", {})
                d["_id"] = row.get("id")
                films.append(d)
            if len(page) < 1000:
                break
            offset += 1000
    return films

def est_domaine_cible(url):
    if not url:
        return False
    u = url.lower()
    return any(dom in u for dom in DOMAINES_CIBLES)

async def verifier_url(semaphore, context, url):
    async with semaphore:
        page = await context.new_page()
        try:
            # On bloque uniquement les images et vidéos lourdes, mais on garde tout le reste pour Transfer.it
            await page.route("**/*.{png,jpg,jpeg,webp,mp4,mkv,avi}", lambda r: r.abort())
            u = url.lower()

            if "transfer.it" in u:
                # Transfer.it met plus de temps à décrypter les métadonnées en JavaScript
                response = await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(6)
            else:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(3.5)

            if response and response.status in [404, 410]:
                return f"Erreur HTTP {response.status}"

            # Si Transfer.it redirige vers la page d'accueil (sans "/t/" dans l'URL finale), le lien est mort
            if "transfer.it" in u and "/t/" not in page.url.lower():
                return "Redirigé vers l'accueil (Lien expiré)"

            texte = (await page.inner_text("body")).lower()

            # 1. Vérification des mots-clés d'expiration
            mots = MOTS_CLES_MORT["gofile.io"] if "gofile.io" in u else (MOTS_CLES_MORT["transfer.it"] if "transfer.it" in u else MOTS_CLES_MORT["google"])
            for mot in mots:
                if mot in texte:
                    return f"Expiré ({mot})"

            # 2. Sécurité spéciale Transfer.it : vérifier qu'un fichier ou bouton Download est bien présent
            if "transfer.it" in u:
                indices_fichier_actif = [
                    "download", "télécharger", "telecharger", "descargar",
                    ".mkv", ".mp4", ".avi", ".zip", ".rar", ".iso"
                ]
                a_un_bouton_ou_fichier = any(ind in texte for ind in indices_fichier_actif)
                a_une_taille = bool(re.search(r'\b\d+([.,]\d+)?\s*(mb|gb|tb|kb|mo|go|to|ko|bytes|octets)\b', texte))

                if not (a_un_bouton_ou_fichier or a_une_taille):
                    # Extrait des 60 premiers caractères affichés sur la page pour te dire exactement ce que Transfer.it affiche
                    apercu = " ".join(texte.split())[:60]
                    return f"Aucun fichier détecté sur Transfer.it ({apercu}...)"

            return None
        except Exception:
            return "Page inaccessible ou délai dépassé"
        finally:
            await page.close()

async def main():
    films = recuperer_films_supabase()
    carte_urls = {}

    for film in films:
        urls_film = set()
        if film.get("link") and est_domaine_cible(film["link"]):
            urls_film.add(film["link"].strip())
        for l in film.get("links", []):
            if isinstance(l, dict) and l.get("url") and est_domaine_cible(l["url"]):
                urls_film.add(l["url"].strip())

        for u in urls_film:
            if u not in carte_urls:
                carte_urls[u] = []
            carte_urls[u].append({
                "id": film.get("_id"),
                "tmdb_id": film.get("tmdb_id"),
                "title": film.get("title", "Sans titre"),
                "poster": film.get("poster", ""),
                "quality": film.get("quality", "")
            })

    semaphore = asyncio.Semaphore(CONCURRENCE_MAX)
    resultats_urls = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        urls_uniques = list(carte_urls.keys())
        taches = [verifier_url(semaphore, context, u) for u in urls_uniques]
        raisons = await asyncio.gather(*taches)

        for u, raison in zip(urls_uniques, raisons):
            if raison is not None:
                resultats_urls[u] = raison
        await browser.close()

    films_morts = []
    for u, raison in resultats_urls.items():
        for info in carte_urls[u]:
            films_morts.append({**info, "dead_url": u, "reason": raison})

    rapport = {
        "last_check": datetime.now().strftime("%d/%m/%Y à %H:%M"),
        "total_movies_scanned": len(films),
        "unique_links_checked": len(carte_urls),
        "dead_links_count": len(resultats_urls),
        "affected_movies_count": len(films_morts),
        "dead_movies": films_morts
    }

    with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
        json.dump(rapport, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
