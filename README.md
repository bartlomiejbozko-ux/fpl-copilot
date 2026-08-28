# FPL Copilot ⚽️

Darmowe, samoaktualizujące się narzędzie analityczne do Fantasy Premier League.
Ciągnie prawdziwe dane z **oficjalnego API FPL** (bez klucza, bez logowania), liczy
prognozy punktów (xPts), buduje ticker FDR i mapę koncentracji ryzyka — i odświeża
się samo co kilka godzin przez GitHub Actions. Hosting na GitHub Pages. Koszt: **0 zł**.

Trzy widoki:
- **Skład** — boisko z xPts na każdego zawodnika, kapitan, kliknięcie → rozbicie na warstwy (forma × minuty × FDR × dom/wyjazd) + pogoda + news.
- **Terminarz FDR** — kolejne 5 kolejek, oficjalny FDR, pogrupowane po klubie.
- **Koncentracja** — na ilu meczach wisi Twoja jedenastka i gdzie kumuluje się ryzyko.

---

## Szybki start (≈10 minut)

1. **Skopiuj repo do siebie** — kliknij *Use this template* albo *Fork* (lub wgraj pliki do nowego repozytorium).

2. **Wpisz swoje ID drużyny.** Otwórz `config.json` i zmień `team_id`:
   ```json
   { "team_id": "3607573", "manual_squad": [] }
   ```
   Numer znajdziesz na `fantasy.premierleague.com` — wejdź na swoją drużynę, ID jest
   w adresie po `/entry/`, np. `.../entry/3607573/event/1` → Twoje ID to `3607573`.

3. **Włącz uprawnienia Actions.** Settings → Actions → General → *Workflow permissions*
   → zaznacz **Read and write permissions** → Save.

4. **Odpal fetcher.** Zakładka **Actions** → *Update FPL data* → *Run workflow*.
   Po chwili w repo pojawi się/zaktualizuje `data.json`.

5. **Włącz stronę.** Settings → **Pages** → Source: *Deploy from a branch* →
   gałąź `main`, katalog `/ (root)` → Save. Po minucie strona żyje pod
   `https://TWOJ-LOGIN.github.io/NAZWA-REPO/`.

Gotowe. Od teraz Action odświeża dane co 3 godziny automatycznie.

---

## Ważne: aktualizacja po transferach

Publiczne API FPL udostępnia tylko skład **zablokowany po ostatnim deadline** —
Twoje oczekujące transfery na najbliższą kolejkę są widoczne dopiero po zalogowaniu.
Dlatego tuż po zrobieniu transferu apka nadal pokazuje poprzedni skład.

**Trzy sposoby, w zależności od tego, ile zachodu chcesz:**

**A) Nic nie rób (domyślne).** Po przejściu deadline'u apka **sama** pobierze nowy,
zablokowany skład (z Twoimi transferami). Zawsze pokazuje drużynę z ostatniego deadline'u.

**B) Ręcznie (`manual_squad`) — natychmiast, prosto.** Wpisz aktualną 15-tkę do
`config.json` (patrz niżej). Nadpisuje API, więc widzisz dokładnie to, co chcesz —
także oczekujące transfery. Minus: po każdym transferze trzeba poprawić listę.

**C) Zalogowana synchronizacja (`FPL_SESSION`) — automatycznie, z oczekującymi transferami.**
Robot pobiera Twój żywy skład przez endpoint `my-team`. Ustawienie:
1. Zaloguj się na `fantasy.premierleague.com` w przeglądarce.
2. Otwórz narzędzia deweloperskie (F12) → **Application/Storage → Cookies** →
   skopiuj wartość ciasteczka **`sessionid`**.
3. Na GitHubie: **Settings → Secrets and variables → Actions → New repository secret**,
   nazwa **`FPL_SESSION`**, wartość = skopiowany `sessionid` → Add secret.
4. Odpal workflow. Robot użyje zalogowanej drużyny.

   Uwagi: ciasteczko `sessionid` **wygasa** po pewnym czasie — wtedy trzeba je odświeżyć.
   FPL bywa też wrażliwy na zapytania z serwerów (GitHub Actions), więc ta metoda może
   nie zawsze zadziałać — wtedy robot automatycznie wraca do publicznego składu (opcja A).
   To ciasteczko to Twój token sesji — trzymaj je tylko w Secrets, nigdy w kodzie repo.

---

## Nie znasz ID? Masz tylko screenshot?

Wpisz nazwiska (dokładnie jak `web_name` na stronie FPL) do `manual_squad`
— pierwszy = kapitan, drugi = wicekapitan:
```json
{
  "team_id": "",
  "manual_squad": ["Donnarumma","Calafiori","White","Gvardiol","Ødegaard",
                   "Rogers","Palmer","B.Fernandes","Szoboszlai","Isak","João Pedro",
                   "Tzolakis","Kayode","De Cuyper","Barry"]
}
```
Fetcher dopasuje nazwiska do zawodników FPL. (Podanie `team_id` jest lepsze —
wtedy dostajesz też prawdziwego kapitana, ławkę, bank i wartość drużyny.)

---

## Co potrafi (wszystko za darmo, z API FPL)

**Doradca** (zakładka główna):
- Kapitan-optymalizator — potwierdza opaskę albo wskazuje lepszego.
- Ustawienie i ławka — liczy optymalną jedenastkę (wszystkie formacje), sugeruje kogo z ławki wystawić i kolejność auto-zmian.
- Rekomendacje transferów — przeszukuje całą bazę, znajduje najlepsze OUT→IN w budżecie.
- Alerty — kontuzje, zawieszenia, wątpliwości (z API).

**Skład** — boisko z xPts, po kliknięciu rozbicie na warstwy + stałe fragmenty (karne/rożne/wolne) + ruch ceny + pogoda.

**Terminarz FDR** — 5 kolejek do przodu, oficjalny FDR, po klubie.

**Koncentracja** — na ilu meczach wisi jedenastka.

**Planer**:
- Doradca chipów — kiedy Bench Boost / Triple Captain / Wildcard / Free Hit (heurystyki).
- Radar DGW/BGW — podwójne i puste kolejki na horyzoncie.
- Różnicowi — wysokie xPts przy niskiej własności (<10%).
- Ruchy cenowe — momentum transferów (przybliżone).
- Skauting rywali — analiza cudzych drużyn (dodaj ich ID do `rivals` w config.json).


**Moja liga**:
- Trajektoria sezonu — punkty, ranking i wartość drużyny kolejka po kolejce (wykres).
- Twoje miniligi — pozycja i ruch w górę/dół (auto z API, bez ręcznych ID).
- Tabele prywatnych lig — czołówka + Twoje otoczenie, z podświetleniem Ciebie.

W szczegółach zawodnika doszła też **mini-forma (ostatnie 5 GW)**, a w Planerze **ranking wartości (xPts za £1m)**.

**Symulator** — „co jeśli": wybierasz OUT/IN, widzisz zysk xPts na 3 kolejki i czy zwraca się −4.

Uczciwie: **xPts, rekomendacje i doradca chipów to heurystyki**, nie model ML ani przewidywane składy.
Kontuzje masz z API; rotacje taktyczne i media wymagałyby płatnych źródeł.

## Jak to działa

```
config.json ──► fetch_data.py ──► data.json ──► index.html
   (Twoje ID)     (GitHub Action        (statyczny        (przeglądarka)
                   co 3h)                plik z danymi)
```

- **`fetch_data.py`** — pobiera `bootstrap-static` (zawodnicy, ceny, forma, minuty),
  `fixtures` (oficjalny FDR), Twoje `picks`, oraz pogodę z Open-Meteo. Liczy xPts
  i zapisuje `data.json`.
- **GitHub Action** pobiera dane po stronie serwera — dzięki temu omijamy problem
  CORS (przeglądarka nie ma bezpośredniego dostępu do API FPL).
- **`index.html`** czyta gotowy `data.json` i rysuje interfejs.

## Model xPts — uczciwie

xPts to **model heurystyczny**, nie oficjalny:
```
baza(0.5·forma + 0.3·PPG + 0.2·sufit_pozycji) × szansa_minut × trudność(FDR) × dom/wyjazd
```
Jest przejrzysty (widzisz każdą warstwę w zakładce Skład) i liczony z prawdziwych
danych, ale to nie jest model ML trenowany latami. Dla porównania pokazujemy obok
`ep_next` — własną prognozę FPL. Traktuj xPts jako drugą opinię, nie wyrocznię.

## Czego (jeszcze) nie ma za darmo
- **Przewidywany skład / rotacje** — brak czystego darmowego API. Pole `chance_of_playing`
  z FPL łapie tylko potwierdzone kontuzje, nie taktyczne rotacje.
- **Sygnał z mediów** — wymagałby API newsowego + streszczania. Do dołożenia później.

## Dostosowanie
- Liczba kolejek w tickerze: stała `HORIZON` w `fetch_data.py`.
- Wagi modelu xPts: funkcja `compute_xpts` w `fetch_data.py`.
- Częstotliwość odświeżania: `cron` w `.github/workflows/update.yml`.

---

Dane: © Premier League (oficjalne API FPL) + Open-Meteo. Narzędzie nieoficjalne,
niepowiązane z Premier League. Do użytku prywatnego.
