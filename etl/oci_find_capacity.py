"""Auto-provisioner per l'istanza ARM Always Free di Oracle Cloud.

Il problema: la shape gratis `VM.Standard.A1.Flex` (Ampere ARM) e' quasi
sempre satura nelle region popolari → `launch_instance` torna
`Out of host capacity` (ServiceError 500). La soluzione manuale e' ricliccare
"Create" all'infinito sperando che una availability domain liberi una A1.
Questo script automatizza quel retry: cicla sulle AD con backoff finche' una
non accetta la richiesta, poi (opzionale) ti avvisa su Telegram.

Struttura ad alto livello:
    1. autenticazione   -> ~/.oci/config (profilo DEFAULT) o instance principals
    2. raccolta config  -> OCID di compartment/subnet/image + chiave SSH (env o CLI)
    3. discovery AD     -> se non passate, le scopre tutte nella tenancy
    4. loop di retry    -> launch_instance per AD; su "out of capacity" aspetta
                           e ritenta; su altri errori si ferma subito
    5. successo         -> stampa OCID + IP pubblico e notifica Telegram

Perche' un loop e non un cron: il provisioning va tentato in modo aggressivo
(ogni 30-90s) finche' non parte, poi si chiude. Non e' un task ricorrente, e'
un "tieni premuto il pulsante per me".

Prerequisiti:
    - OCI Python SDK:  `uv run --with oci python etl/oci_find_capacity.py ...`
      (non e' tra le dipendenze di Boardy: e' uno script ops one-shot)
    - File ~/.oci/config con un profilo valido (API key generata dal dashboard
      OCI -> Profile -> API Keys). In OCI Cloud Shell e' gia' tutto pronto.

Esempio:
    uv run --with oci python etl/oci_find_capacity.py \
        --compartment ocid1.compartment.oc1..xxxx \
        --subnet      ocid1.subnet.oc1.eu-milan-1.xxxx \
        --image       ocid1.image.oc1.eu-milan-1.xxxx \
        --ssh-key     ~/.ssh/id_ed25519.pub \
        --ocpus 4 --memory-gb 24 \
        --interval 60 --notify-telegram

Tutti i parametri --foo hanno un fallback su variabile d'ambiente OCI_FOO
(es. --compartment <- OCI_COMPARTMENT) cosi' puoi metterli nel .env e lanciare
lo script nudo.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
import urllib.parse
import urllib.request

# Windows: cp1252 va in crash sulle frecce/spunte dei log. Forziamo UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


# ---------------------------------------------------------------------------
# Telegram notify (stdlib, niente dipendenze): riusa le stesse env del bot.
# ---------------------------------------------------------------------------
def notify_telegram(text: str) -> None:
    """Manda `text` al primo owner in TELEGRAM_OWNER_IDS. Best-effort."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_ids = (os.environ.get("TELEGRAM_OWNER_IDS", "") or "").split(",")
    chat_id = next((o.strip() for o in owner_ids if o.strip()), None)
    if not token or not chat_id:
        print("  [telegram] TELEGRAM_BOT_TOKEN/OWNER_IDS mancanti, salto la notifica.")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            resp.read()
        print("  [telegram] notifica inviata.")
    except Exception as exc:  # best-effort: la notifica non deve far fallire lo script
        print(f"  [telegram] notifica fallita: {exc}")


# ---------------------------------------------------------------------------
# Config: ogni flag CLI ha fallback su env OCI_<NAME>.
# ---------------------------------------------------------------------------
def _env_default(name: str) -> str | None:
    return os.environ.get(f"OCI_{name.upper()}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Auto-find Oracle ARM Always Free capacity.")
    p.add_argument("--compartment", default=_env_default("compartment"),
                   help="OCID del compartment (env OCI_COMPARTMENT). Default: tenancy root dal config.")
    p.add_argument("--subnet", default=_env_default("subnet"),
                   help="OCID della subnet (env OCI_SUBNET). Obbligatorio.")
    p.add_argument("--image", default=_env_default("image"),
                   help="OCID dell'immagine OS, es. Ubuntu 22.04 ARM (env OCI_IMAGE). Obbligatorio.")
    p.add_argument("--ssh-key", default=_env_default("ssh_key"),
                   help="Path al file .pub della chiave SSH (env OCI_SSH_KEY). Obbligatorio.")
    p.add_argument("--shape", default=_env_default("shape") or "VM.Standard.A1.Flex",
                   help="Shape. Default VM.Standard.A1.Flex (la ARM Always Free).")
    p.add_argument("--ocpus", type=int, default=int(_env_default("ocpus") or 4),
                   help="OCPU (default 4 = tutta l'allocazione Always Free ARM).")
    p.add_argument("--memory-gb", type=int, default=int(_env_default("memory_gb") or 24),
                   help="RAM in GB (default 24 = tutta l'allocazione Always Free).")
    p.add_argument("--name", default=_env_default("name") or "boardy-arm",
                   help="Display name dell'istanza.")
    p.add_argument("--ads", default=_env_default("ads"),
                   help="CSV di availability domain. Se omesso le scopre tutte.")
    p.add_argument("--interval", type=int, default=int(_env_default("interval") or 60),
                   help="Secondi base tra i tentativi (con jitter +-25%%). Default 60.")
    p.add_argument("--max-attempts", type=int, default=int(_env_default("max_attempts") or 0),
                   help="Stop dopo N tentativi totali. 0 = infinito (default).")
    p.add_argument("--profile", default=os.environ.get("OCI_CLI_PROFILE", "DEFAULT"),
                   help="Profilo nel ~/.oci/config. Default DEFAULT.")
    p.add_argument("--instance-principal", action="store_true",
                   help="Autentica via instance principals invece del file config (utile da una VM OCI).")
    p.add_argument("--notify-telegram", action="store_true",
                   help="Manda una notifica Telegram quando l'istanza parte.")
    return p.parse_args()


def build_clients(args):
    """Ritorna (compute_client, identity_client, config_dict, tenancy_ocid)."""
    try:
        import oci  # import locale: dipendenza opzionale
    except ImportError:
        sys.exit("OCI SDK non installato. Lancia con: "
                 "uv run --with oci python etl/oci_find_capacity.py ...")

    if args.instance_principal:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {"region": signer.region}
        tenancy = signer.tenancy_id
        compute = oci.core.ComputeClient(config={}, signer=signer)
        identity = oci.identity.IdentityClient(config={}, signer=signer)
    else:
        config = oci.config.from_file(profile_name=args.profile)
        tenancy = config["tenancy"]
        compute = oci.core.ComputeClient(config)
        identity = oci.identity.IdentityClient(config)
    return compute, identity, config, tenancy


def discover_ads(identity, compartment: str) -> list[str]:
    """Tutte le availability domain del compartment (di solito 1-3)."""
    ads = identity.list_availability_domains(compartment_id=compartment).data
    return [ad.name for ad in ads]


def make_launch_details(args, ad: str, ssh_pub: str):
    """Costruisce LaunchInstanceDetails per una specifica AD."""
    import oci
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=args.compartment,
        shape=args.shape,
        display_name=args.name,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(args.ocpus),
            memory_in_gbs=float(args.memory_gb),
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=args.image),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=args.subnet,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_pub},
    )


def is_capacity_error(exc) -> bool:
    """True se l'errore e' il classico 'Out of host capacity' (da ritentare)."""
    # oci.exceptions.ServiceError: status 500 + code 'InternalError' con
    # messaggio "Out of host capacity", oppure 'LimitExceeded'/'QuotaExceeded'
    # quando l'AD e' temporaneamente piena.
    status = getattr(exc, "status", None)
    msg = (getattr(exc, "message", "") or "").lower()
    code = (getattr(exc, "code", "") or "").lower()
    if "out of host capacity" in msg or "out of capacity" in msg:
        return True
    if status == 500 and code in ("internalerror", "outofcapacity"):
        return True
    return False


def main() -> None:
    args = parse_args()

    # Validazione minima dei parametri obbligatori.
    missing = [n for n in ("subnet", "image", "ssh_key")
               if not getattr(args, n.replace("-", "_"))]
    if missing:
        sys.exit(f"Parametri obbligatori mancanti: {', '.join(missing)} "
                 f"(passali via --{missing[0]} o env OCI_{missing[0].upper()}).")

    ssh_path = os.path.expanduser(args.ssh_key)
    if not os.path.isfile(ssh_path):
        sys.exit(f"Chiave SSH non trovata: {ssh_path}")
    with open(ssh_path, encoding="utf-8") as fh:
        ssh_pub = fh.read().strip()

    import oci  # per intercettare ServiceError
    compute, identity, config, tenancy = build_clients(args)

    # Compartment: default = tenancy root se non specificato.
    if not args.compartment:
        args.compartment = tenancy
        print(f"--compartment non passato: uso la tenancy root {tenancy[:24]}...")

    ads = [a.strip() for a in args.ads.split(",")] if args.ads else discover_ads(identity, args.compartment)
    if not ads:
        sys.exit("Nessuna availability domain trovata.")

    region = config.get("region", "?")
    print(f"Region: {region} | Shape: {args.shape} {args.ocpus}OCPU/{args.memory_gb}GB")
    print(f"Availability domains da provare ({len(ads)}): {', '.join(ads)}")
    print(f"Intervallo base: {args.interval}s | Max tentativi: {args.max_attempts or 'infinito'}\n")

    attempt = 0
    while True:
        for ad in ads:
            attempt += 1
            ts = time.strftime("%H:%M:%S")
            try:
                details = make_launch_details(args, ad, ssh_pub)
                resp = compute.launch_instance(details)
                inst = resp.data
                print(f"\n[{ts}] ISTANZA CREATA in {ad}!")
                print(f"  OCID: {inst.id}")
                print(f"  Stato: {inst.lifecycle_state}")
                msg = (f"Boardy ARM: istanza creata in {ad} "
                       f"({args.ocpus}OCPU/{args.memory_gb}GB).\nOCID: {inst.id}")
                if args.notify_telegram:
                    notify_telegram(msg)
                print("\nProssimo passo: aspetta che lo stato sia RUNNING, poi "
                      "recupera l'IP pubblico dal dashboard OCI o con "
                      "`oci compute instance list-vnics`.")
                return
            except oci.exceptions.ServiceError as exc:
                if is_capacity_error(exc):
                    print(f"[{ts}] tentativo #{attempt} {ad}: out of capacity, ritento.")
                else:
                    # Errore "vero" (config, permessi, quota superata davvero,
                    # subnet/image sbagliata): inutile insistere.
                    print(f"\n[{ts}] Errore non recuperabile su {ad}:")
                    print(f"  status={exc.status} code={exc.code}")
                    print(f"  message={exc.message}")
                    sys.exit(1)

            if args.max_attempts and attempt >= args.max_attempts:
                print(f"\nRaggiunto il limite di {args.max_attempts} tentativi senza successo.")
                if args.notify_telegram:
                    notify_telegram(f"Boardy ARM: {args.max_attempts} tentativi falliti, "
                                    f"ancora out of capacity.")
                sys.exit(2)

        # Backoff con jitter +-25% per non martellare l'API a cadenza fissa.
        wait = args.interval * random.uniform(0.75, 1.25)
        time.sleep(wait)


if __name__ == "__main__":
    main()
