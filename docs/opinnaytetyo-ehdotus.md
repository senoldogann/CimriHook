**Aihe:** Opinnäytetyön aihe-ehdotus: CimriHook – tekoälykoodausagenttien token-kustannusten mittaaminen ja hallinta

Hei [opettajan nimi],

olen [Nimi], tieto- ja viestintätekniikan kolmannen vuoden opiskelija (ryhmä [ryhmätunnus]). Aloittelen opinnäytetyötäni, ja alla on lyhyt kuvaus aiheestani ennen varsinaista aloitusta.

**Alustava otsikko:** CimriHook – tekoälykoodausagenttien kontekstinhallinta token-kustannusten vähentämiseksi laatua heikentämättä
(engl. *CimriHook – Context Management for Reducing the Token Cost of AI Coding Agents without Quality Loss*)

**Mitä?**
Kehitän ja arvioin avoimen lähdekoodin järjestelmän, joka vähentää tekoälykoodausagenttien token-kustannuksia. Kohteina ovat Claude Code ja OpenAI Codex CLI. Järjestelmässä on kolme tasoa:
1. **Mittaus.** Työkalu analysoi agentin todelliset istuntolokit ja toistaa samat pyyntöketjut eri kontekstinhallintapolitiikoilla. Simulaattori laskee kustannukset palveluntarjoajan välimuistihinnoilla, ja sen tulokset poikkeavat todellisista laskutustiedoista alle 1,1 %.
2. **Hallinta.** Järjestelmä valitsee käyttäjän omasta datasta kustannuksiltaan edullisimman kontekstikoon ja ottaa sen käyttöön agentin omalla asetuksella. Välityspalvelinta ei tarvita, joten ratkaisu toimii myös kuluttajatilauksilla.
3. **Uudelleenkoodaus.** Claude Code -hookit estävät lähettämästä agentille tietoa, joka on jo sen kontekstissa:
   - täsmälleen sama tulos korvataan viittauksella
   - muuttuneesta tuloksesta lähetetään vain muutokset (diff)
   - suuresta, ennen näkemättömästä tiedostosta lähetetään ensin sen rakenne.

Ratkaisu toimii työkalu- ja asetuskerroksessa, joten se ei riipu käytetystä mallista eikä päättelytasosta (effort).

**Miksi?**
- **Konteksti luetaan joka kerta uudelleen.** Agentti lukee koko kontekstinsa jokaisella API-pyynnöllä. Analysoin esiselvityksenä omat istuntoni 30 päivän ajalta:
  - Claude Code: noin 29 700 pyyntöä ja 9,3 miljardia syötetokenia. Tokeneista noin 98 % luettiin välimuistista, ja keskimääräinen konteksti oli 320 000 tokenia.
  - Codex: noin 6 000 pyyntöä, välimuistista luettu osuus 96,5 % ja keskimääräinen konteksti 119 000 tokenia.
- **Tulosteiden tiivistäminen auttaa vain vähän.** Nykyiset työkalut keskittyvät yksittäisten tulosteiden tiivistämiseen, mutta tuoreen tutkimuksen mukaan se vaikuttaa laskuun vähän. Esimerkiksi RTK pienensi Claude Coden laskutettua kustannusta lyhyissä tehtävissä vain 2,7 %, koska välimuistin luku ja kirjoitus muodostavat noin 80 % laskusta (arXiv 2607.12161).
- **Kontekstin keston hallinta osuu suurimpaan kuluerään.** JetBrainsin ja TUM:n tutkimuksessa (arXiv 2508.21433) vanhojen työkalutulosten piilottaminen puolitti SWE-bench-tehtävien kustannukset useimmissa kokoonpanoissa, eikä ratkaisuprosentti laskenut.
- **Ensimmäiset todelliset mittaukset tukevat tätä.** Rakensin A/B-testiympäristön, joka ajaa samat koodaustehtävät omilla Claude- ja ChatGPT-tilauksillani kahdella tavalla: oletusasetuksilla ja CimriHookin kanssa.
  - **Tehtävät:** oikeita avoimen lähdekoodin projekteja, joihin on lisätty virheitä. Onnistuminen mitataan projektin omilla testeillä.
  - **Tulokset:** esikokeessa ajoin 16 pitkää, 20-vaiheista istuntoa. Kustannus pieneni Codexissa 24 % (95 %:n luottamusväli 16–33 %) ja Claude Codessa 8 % (6–11 %). Kaikki 160 vaihetta onnistuivat molemmissa ryhmissä.
  - **Säästö kasvaa kontekstin mukana:** omien todellisten Claude Code -istuntojeni simulaatio (keskimääräinen konteksti 320 000 tokenia) ennustaa 23–33 %:n säästöä.

**Tavoitteet**
1. Mittausmenetelmä ja avoin työkalu kahdelle agentille: kustannusten erittely ja kalibroitu politiikkasimulaattori.
2. Toimiva prototyyppi, jossa on käyttäjäkohtainen kontekstinhallinta ja työkalutulosten uudelleenkoodaus.
3. A/B-arviointi vakioiduilla koodaustehtävillä:
   - Tavoite: pitkissä istunnoissa kokonaiskustannus pienenee vähintään 15 % kummallakin agentilla, eikä tehtävien onnistumisprosentti (testit menevät läpi) laske yli 5 prosenttiyksikköä.
   - Vertailu tehdään vähintään kahdella mallilla ja kahdella effort-tasolla.
   - Vertailu tehdään sekä RTK:n kanssa että ilman (2×2-asetelma).
4. Hookin viive alle 100 ms työkalukutsua kohden. Prototyypissä mediaani on nyt noin 76 ms.

**Vaatimukset**
Toiminnalliset:
- Tietoa ei menetetä. Uudelleenkoodattu tulos kertoo, miten alkuperäisen saa, ja kun pyyntö toistetaan, agentti saa alkuperäisen tuloksen.
- Kontekstia seurataan erikseen pääkeskustelulle ja jokaiselle ala-agentille, ja seuranta nollataan tiivistyksen yhteydessä.
- Suositukset perustuvat käyttäjän omaan dataan, ja simulaattori raportoi oman kalibrointivirheensä.
- Säästöt raportoidaan istunnoittain ja mekanismeittain.

Ei-toiminnalliset:
- Ei ajonaikaisia riippuvuuksia (vain Pythonin standardikirjasto). Toimii macOS:ssä ja Linuxissa.
- Vikasietoisuus: jos järjestelmä kaatuu, agentti jatkaa alkuperäisellä tuloksella.
- Palveluntarjoajien käyttöehtoja noudatetaan. Tunnistetietoja ei välitetä eteenpäin, eikä kuluttajatilauksilla käytetä välityspalvelinta.
- Koodi on tyypitetty (mypy strict) ja katettu automaattisilla testeillä. Lisenssi on MIT.

**Rajaukset**
- **Agentit:** Claude Code ja Codex CLI. Mittaus ja kontekstinhallinta toteutetaan molemmille, uudelleenkoodaus vain Claude Codelle, koska Codexin hookit eivät salli työkalun tulosteen muokkaamista.
- **Ei välityspalvelinta:** Palvelinpuolen kontekstin muokkaus API-avaimella (esim. Anthropicin context editing) jätetään jatkotutkimukseen.
- **Rajattu pois:** mallien koulutus ja kielimallipohjainen kehotteiden tiivistys.
- **Arvioinnin laajuus:** kustannussyistä noin 20–30 tehtävää.

**Menetelmä ja alustava aikataulu**
Menetelmänä on kehittämistutkimus (design science): esiselvitys ja mittaus, toteutus, A/B-arviointi ja johtopäätökset.
- Viikot 1–3: teoria ja mittausmenetelmä
- Viikot 4–8: toteutus
- Viikot 9–11: arviointi
- Viikot 12–14: raportin kirjoittaminen

Toimeksiantaja: [täydennä]. Prototyypin ensimmäinen versio toimii jo (mittaus, simulaattori, kontekstinhallinta, uudelleenkoodaus ja A/B-testiympäristö): [repositorion linkki].

Ystävällisin terveisin
[Nimi]
[Opiskelijanumero]
