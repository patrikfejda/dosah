# MVP špecifikácia — „Dosah" (Kam sa dostanem?)

## 1. Core user flow (jedna obrazovka)

1. Používateľ otvorí appku → celoobrazovková mapa Bratislavy (Leaflet, OSM), marker štartu stojí na Hlavnom námestí. Nad mapou pláva ovládací panel (karta vľavo hore).
2. Aplikácia hneď po načítaní automaticky vypočíta default izochronu (Pešo, 15 min) — používateľ vidí výsledok bez jediného kliknutia.
3. Používateľ mení režim / čas / štart → každá zmena spustí prepočet (debounce 400 ms na slideri). Počas výpočtu sa panel prepne do stavu „Počítam…" so spinnerom; mapa ostáva interaktívna.
4. Výsledok: farebný polygón na mape + info riadok (plocha, čas výpočtu). Mapa sa auto-fitne na polygón (raz po každom prepočte, s paddingom).

Žiadne ďalšie obrazovky, modaly ani onboarding.

## 2. UI ovládacie prvky (presné slovenské labely)

| Prvok | Špecifikácia |
|---|---|
| Nadpis panelu | **„Kam sa dostanem?"** |
| Prepínač režimu | Segmentový toggle: **Pešo · Bicykel · MHD · Auto**. Default: **Pešo**. |
| Časový slider | Label **„Čas: {N} min"** (nad hodinu „{H} h {M} min"). Rozsah **5 min – 10 h**, logaritmická stupnica (jemný krok pri krátkych časoch), default **15 min**. |
| Štart | Label **„Štart: klikni na mapu"** + tlačidlo **„📍 Moja poloha"** (browser geolocation; pri zamietnutí toast „Poloha nedostupná, klikni na mapu."). Default: Hlavné námestie, BA (48.1436, 17.1093). Klik na mapu presunie marker. Marker je draggable. |
| Odchod (len MHD) | Zobrazí sa iba pri režime MHD. Label **„Odchod:"** + `<input type="time">` + checkbox **„Teraz"** (default zaškrtnutý → aktuálny čas; pri odškrtnutí sa použije hodnota z inputu). Deň = dnešný (žiadny výber dátumu). |
| Info riadok | Pod ovládaním: **„Plocha: {X} km² · vypočítané za {Y} s"**. |
| Chybový stav | Červený banner v paneli: **„Nepodarilo sa vypočítať dosah. Skús znova."** + tlačidlo **„Skúsiť znova"**. |

## 3. Zobrazenie výsledku

- **Polygón**: semi-transparentná výplň (opacity ~0.35) + plný obrys. Farba podľa režimu: Pešo zelená #2e7d32, Bicykel oranžová #ef6c00, MHD modrá #1565c0, Auto červená #c62828. Vždy len jedna izochrona naraz — žiadne vrstvenie pásiem.
- **Plocha v km²** zaokrúhlená na 1 desatinné miesto (počítaná na backende zo zunionovaného polygónu).
- **Čas výpočtu** v sekundách na 1 desatinné miesto.
- **Legenda**: netreba samostatnú — toggle tlačidlá nesú farebnú bodku vo farbe polygónu.
- **Atribúcia** (povinná, Leaflet attribution control vpravo dole): © Prispievatelia OpenStreetMap | Trasy: Valhalla | MHD: GTFS. Odkaz na openstreetmap.org/copyright klikateľný.
- Marker štartu vždy viditeľný nad polygónom.

## 4. Akceptačné kritériá

1. **Autoštart**: Po otvorení stránky sa do 10 s bez interakcie zobrazí zelený polygón (Pešo, 15 min) okolo Hlavného námestia s vyplneným info riadkom.
2. **Klik = nový štart**: Kliknutie kamkoľvek na mapu presunie marker a automaticky spustí prepočet; starý polygón zmizne najneskôr pri zobrazení nového.
3. **Slider s debounce**: Ťahanie slideru spustí prepočet až po pustení/pauze; väčší čas dá viditeľne väčší polygón a väčšie km².
4. **MHD sanity check**: MHD, 30 min, odchod v pracovný deň o 8:00, štart Hlavná stanica → polygón pokrýva aspoň časť Petržalky a Ružinova a plocha je výrazne väčšia než Pešo 30 min z toho istého bodu.
5. **Monotónnosť režimov**: Z rovnakého štartu a času platí plocha(Auto) > plocha(Bicykel) > plocha(Pešo).
6. **Odchodový čas mení výsledok**: MHD izochrona pre odchod 8:00 a 2:00 z toho istého bodu sa líši (nočná je menšia alebo iná).
7. **Chybový stav**: Pri nedostupnom Valhalla API sa zobrazí banner „Nepodarilo sa vypočítať dosah. Skús znova.", appka nespadne a „Skúsiť znova" zopakuje request.
8. **Geolokácia**: „📍 Moja poloha" pri udelenom povolení presunie marker a prepočíta; pri zamietnutí toast a marker sa nehýbe.

## 5. Mimo rozsahu (nebudovať)

- Vrstvenie viacerých izochron / porovnanie režimov naraz
- Geocoding adresy — štart len klik/geolokácia
- Výber dátumu pre MHD (len dnešok), sviatky, výluky, real-time meškania
- Zdieľanie URL, export GeoJSON/PNG, tlač
- Ukladanie histórie, obľúbené miesta, účty
- Mobilná optimalizácia nad rámec „panel sa nezlomí"
- POI vo vnútri polygónu, štatistiky obyvateľstva
- ~~Iné štartovacie mestá než Bratislava a okolie~~ (od 17. 9. štart kdekoľvek v pokrytí grafu: SK + AT + CZ + HU + juh PL)
- Multimodálne kombinácie, parkovanie, spiatočná cesta
- Dark mode, i18n prepínač jazyka

## 6. Názov a tagline

**Názov: Dosah** — *„Zadaj minúty. Uvidíš, kam až siahaš."*
