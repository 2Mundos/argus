# Runbook — provar o sandbox interno do Codex com as políticas candidatas

> **WIP / CANDIDATO.** O opt-in por worker **está implementado** (passo 7), mas as políticas **não foram
> carregadas nem testadas em runtime** e **nada aqui autoriza deploy ou ativação**. Ligar a pool só depois do
> `ALL-AS-EXPECTED` do passo 3 na imagem que será usada. **Estado real:** o probe com o perfil v1 (enforce) falhou no
> positivo (`bwrap: Failed to make / slave`, kernel: `flags="rw, silent, rslave"`); o perfil v2 corrige a regra mas
> **ainda não foi carregado nem testado**. Nada passou em runtime. Contrato: [spec](../../../docs/specs/codex-sandbox-profile.spec.md).

Operador executa (precisa de sudo; a sessão do agente não tem: `sudo -n` →
"interactive authentication is required"). **Nada abaixo foi executado pelo agente**, exceto os passos 0 e 7a
(offline). Os passos 1, 3, 4 e 6 exigem root/Docker e continuam pendentes.
Rode da raiz `integrations/nanoclaw/security/`. Imagem:
`argus-nanoclaw-agent:radar-74b160e-deadline7200`. Sem `--privileged`, sem `SYS_ADMIN`, sem sysctl
global, sem `unconfined`, sem reiniciar o Docker.

## 0. Validação offline (sem root)
```bash
scripts/validate-offline.sh
scripts/verify-install.sh /caminho/do/checkout-nanoclaw-git   # fixture descartável: install.sh 2x + tsc + testes do driver
```

## 1. Instalar e carregar o perfil (root)
```bash
sudo apparmor_parser -QK -S argus-codex-worker.apparmor >/dev/null   # revalida como root
sudo install -m 0644 -o root -g root argus-codex-worker.apparmor /etc/apparmor.d/argus-codex-worker
sudo apparmor_parser -r -W /etc/apparmor.d/argus-codex-worker
sudo aa-status | grep -n argus-codex-worker      # deve listar em "profiles are in enforce mode"
```
Persistência no boot: `/etc/apparmor.d/` é relido pelo `apparmor.service`. Perfil não carregado ⇒ o
container **não inicia** (o runc falha no `start`, não no `create`; falha fechada, sem cair para o default).

Recarga da v1 para a v2 (mesmos comandos; `-r` substitui o perfil carregado, sem reiniciar o Docker). Containers
já em execução com o perfil continuam com o carregado até serem recriados. Depois do reload confirme o modo:
```bash
sudo aa-status | grep -n argus-codex-worker       # "enforce"; se aparecer em "complain", o código recusa o spawn
```

## 2. Preparar workspace descartável e canários (sem credenciais, sem host root)
```bash
export PROBE=$(mktemp -d "${TMPDIR:-$HOME/.hermes/cache/scratch}/argus-codex-probe.XXXXXX")
mkdir -p "$PROBE/ws" "$PROBE/other" && chmod 0777 "$PROBE/ws" "$PROBE/other"
export IMG=argus-nanoclaw-agent:radar-74b160e-deadline7200
```

## 3. Rodar o probe (container descartável, sem rede, caps ALL dropped, no-new-privileges)
```bash
set -o pipefail   # sem isto, `| tee` mascara o código de saída do probe
docker run --rm --init --name argus-codex-probe \
  --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt apparmor=argus-codex-worker \
  --security-opt seccomp="$PWD/argus-codex-worker.seccomp.json" \
  --pids-limit 256 --memory 512m --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  -v "$PROBE/ws:/workspace/group" -v "$PROBE/other:/workspace/other" \
  -v "$PWD/scripts/probe.sh:/probe.sh:ro" \
  --entrypoint /bin/bash "$IMG" /probe.sh 2>&1 | tee "$PROBE/probe.log"
```
Forma do comando Codex: no 0.162.1 o fonte (`cli/src/main.rs`, testes de parse) usa
`codex sandbox --permission-profile :workspace -- <cmd>`; a forma anterior
`sandbox linux --full-auto` não é subcomando nesta versão (o `codex 0.163.0` do host respondeu
`Failed to execvp linux`). O probe imprime `sandbox --help` para você confirmar.

### Saída esperada
- Cabeçalho: `argus-codex-worker (enforce)`, `Seccomp: 2`, `NoNewPrivs: 1`, `CapEff: 0000…`.
- **Controles** `control-write-*` = PASS (se falharem, os negativos não provam nada).
- `write-workspace` PASS e `write-other/home/etc-denied` PASS + `absent:*` PASS.
- `net-denied` com código impresso; `EPERM/EACCES` ⇒ bloqueio do sandbox; `ENETUNREACH` ⇒ só o
  `--network none` (o Codex em `:workspace` pode ou não bloquear; não afirmar mais que isso).
- Bloco "blast radius": `userns-tmpfs-mount` PASS (aceito pelo candidato), demais mounts PASS = negados.
- Resultado final `ALL-AS-EXPECTED` (exit 0). Exit 1 = alguma checagem falhou; exit 2 = binário do Codex ausente;
  **exit 3 = `BOOTSTRAP-FAILED`**: o sandbox do Codex não subiu, as checagens negativas ficam `NOT_EXECUTED` e **nada foi provado**
  (um negativo só vale se o comando chegou a iniciar; o script exige o marcador). Qualquer FAIL ⇒ não ativar; ver passo 4.
- Rede: `PASS net-blocked-by-sandbox` só com EPERM/EACCES; `INFO net-blocked-only-by-docker-network-none` não prova o sandbox.
- Blast radius usa `unshare -Urm --propagation unchanged` para o próprio `unshare` não tentar montar `/`; se ele nem inicia, as
  checagens ficam `NOT_EXECUTED`.

Se `write-workspace` falhar, o perfil ainda não é suficiente: as regras de mount são candidatas.

## 4. Diagnóstico e estreitamento (root)
```bash
sudo journalctl -k --since "-10min" | grep 'apparmor="DENIED"'   # operation="mount"/"pivot_root", name=, flags=
```
Ajuste só a regra citada em `DENIED`, recarregue (`apparmor_parser -r -W`) e repita o passo 3. Depois de
passar, troque `/**` pelos destinos observados e anexe o log em um relatório `YYYY-MM-DD.*.report.md`.
Falha de seccomp (sem DENIED no kernel, `EPERM`/`ENOSYS`): `strace` exigiria `SYS_PTRACE`; isole com
`unshare -Ur true` e `unshare -Urm sh -c 'mount -t tmpfs t /mnt'` dentro do mesmo `docker run` do passo 3.

## 5. Se NÃO for possível sem relaxar
Se o único caminho for `unconfined`, `--privileged`, `CAP_SYS_ADMIN` ou sysctl global: **parar** e
**manter a tarefa bloqueada** (o worker Codex não roda nessa configuração) e reportar o bloqueio ao
dono. **Não** desativar o sandbox interno do Codex como contorno: isso exigiria decisão do dono em
outro PR/spec. Não relaxe nada aqui.

## 6. Limpeza e rollback (root; só sem dependentes)
```bash
rm -rf "$PROBE"                                           # sem sudo, é seu
# dependentes (rodando OU parados: um start com perfil ausente falha):
docker ps -a --format '{{.Names}}' | xargs -r docker inspect --format '{{.Name}} {{.HostConfig.SecurityOpt}}' | grep argus-codex-worker
# só se a saída acima for vazia:
sudo apparmor_parser -R /etc/apparmor.d/argus-codex-worker && sudo rm /etc/apparmor.d/argus-codex-worker
```

## 7. Ativar por worker (pool) — só depois do passo 3

O opt-in é **uma variável no `env` da pool**, nada global. Valor único `codex-bwrap`; ausente = comportamento
de hoje. Vale só para o container **agente** do provider **`codex`** nativo; gateways/auxiliares, outros
providers e o Codex delegado (`argus-delegate`) não recebem. Não vale para `nanoclaw-sandbox` (daemon interno): recusado.
Ela não marca nada como pronto; é uma flag de operador.

**a) Instalar os artefatos no checkout (sem root, idempotente; é o que o `provision.sh` já roda):**
```bash
integrations/nanoclaw/install.sh /caminho/do/checkout-nanoclaw    # copia o driver e o JSON seccomp para <checkout>/security/
(cd /caminho/do/checkout-nanoclaw && pnpm run build)               # o host roda dist/
```

**b) Preflight (o código faz sozinho; estes são os equivalentes manuais):**
```bash
sudo aa-status | grep -n argus-codex-worker                        # perfil carregado, em "enforce"
docker info --format '{{json .SecurityOptions}}'                   # name=apparmor e name=seccomp… (necessário, NÃO suficiente)
jq -e '.defaultAction=="SCMP_ACT_ERRNO"' /caminho/do/checkout-nanoclaw/security/argus-codex-worker.seccomp.json
```
`docker info` só diz que o AppArmor existe; não prova que o perfil está carregado nem em enforce. Por isso, antes de cada
`docker create` do agente optado (cache de 30 s por imagem), o driver roda **um container descartável no mesmo daemon** com a
imagem e as flags do agente, `--network none --read-only --pull=never`, sem mounts, sem env, sem credenciais, e lê
`/proc/self/attr/current` e `/proc/self/status`. Exige `argus-codex-worker (enforce)`, `NoNewPrivs: 1`, `Seccomp: 2` e `CapEff`
zerado; `complain`, `unconfined`, outro perfil, perfil não carregado (runc `change_onexec … label not found`) ou falha do probe
**recusam** o spawn (negado, sem fallback). Limite: prova o estado no momento do probe, não ao longo da sessão. Um container
de sessão já existente só é adotado se o `docker inspect` (apenas `SecurityOpt`, `CapDrop`, `CapAdd`, `Privileged`,
`AppArmorProfile`, `Init`) mostrar exatamente o perfil, o seccomp idêntico ao JSON instalado e `cap-drop ALL`; senão é recusado e
**não é tocado**. Sem root não há como carregar o perfil; nesse caso **não ligue**.

**c) Ligar uma pool (as instâncias já em execução não são recicladas; vale para hosts criados depois):**
```bash
curl -fsS -X PATCH "$ARGUS_URL/api/pools/<pool>" -H "Authorization: Bearer $ARGUS_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":"<pool.version>","env":{"ARGUS_CODEX_SANDBOX_PROFILE":"codex-bwrap"}}'
```
Valor desconhecido (`true`, `unconfined`, `CODEX-BWRAP`…): o host recusa subir (`entrypoint.sh`, exit 78) e,
se chegar ao spawn, o spawn é negado. Não há fallback para o default nem para `unconfined`.

**d) Rollback de uma pool:** `"env":{"ARGUS_CODEX_SANDBOX_PROFILE":null}` no mesmo `PATCH`; hosts novos voltam ao
default. Containers agentes já criados com o perfil só mudam quando forem recriados. Só descarregue o AppArmor
(passo 6) depois que nenhum container dependente existir.

**Não faça:** `security_opt` arbitrário, `unconfined`, `--privileged`, `SYS_ADMIN`, ligar fora do provider Codex,
ou reciclar workers vivos como parte disto.
