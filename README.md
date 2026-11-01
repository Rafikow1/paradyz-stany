# Stany magazynowe Ceramika Paradyż – Leroy Merlin, Castorama, OBI

Program co noc odczytuje stany produktów Paradyża we wszystkich sklepach trzech sieci
i publikuje je na stronie dla przedstawicieli.

* **Strona:** zakładka *Settings → Pages* pokazuje adres. Dostęp na hasło.
* **Dane są zaszyfrowane** (AES-256). W repozytorium leżą tylko pliki `*.enc` – bez hasła są nieczytelne.
* **Nocny odczyt:** `.github/workflows/nocny-odczyt.yml` (ok. 00:17). Ręcznie: *Actions → Nocny odczyt stanów → Run workflow*.
* **Hasło:** sekret repozytorium `DATA_PASSWORD` (*Settings → Secrets and variables → Actions*).
  Zmiana hasła = zmiana sekretu + ponowne zaszyfrowanie `dane/input.xlsx.enc`.

## Nowa lista produktów / stanów Paradyża
Plik `dane/input.xlsx.enc` to zaszyfrowana „Zbiorcza lista”. Nową listę szyfruje się poleceniem
`python -m utils.publish encrypt lista.xlsx dane/input.xlsx.enc` (z hasłem w `DATA_PASSWORD`).
