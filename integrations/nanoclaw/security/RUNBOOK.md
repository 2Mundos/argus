# Runbook — provar o sandbox interno do Codex com as políticas candidatas

> **WIP / CANDIDATO.** Este runbook não foi executado, as políticas não foram carregadas nem
> testadas em runtime e **nada aqui autoriza deploy ou ativação**. Ativar exige o resultado do passo 3
> e um PR de implementação separado, conforme a [spec](../../../docs/specs/codex-sandbox-profile.spec.md).

Operador executa (precisa de sudo; a sessão do agente não tem: `sudo -n` →
"interactive authentication is required"). **Nada abaixo foi executado pelo agente**, exceto o passo 0.
Rode da raiz `integrations/nanoclaw/security/`. Imagem:
`argus-nanoclaw-agent:radar-74b160e-deadline7200`. Sem `--privileged`, sem `SYS_ADMIN`, sem sysctl
global, sem `unconfined`, sem reiniciar o Docker.

## 0. Validação offline (sem root)
```bash
scripts/validate-offline.sh
```

## 1. Instalar e carregar o perfil (root)
```bash
sudo apparmor_parser -QK -S argus-codex-worker.apparmor >/dev/null   # revalida como root
sudo install -m 0644 -o root -g root argus-codex-worker.apparmor /etc/apparmor.d/argus-codex-worker
sudo apparmor_parser -r -W /etc/apparmor.d/argus-codex-worker
sudo aa-status | grep -n argus-codex-worker      # deve listar em "profiles are in enforce mode"
```
Persistência no boot: `/etc/apparmor.d/` é relido pelo `apparmor.service`. Perfil não carregado ⇒ o
Docker recusa criar o container (falha fechada).

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
- Resultado final `ALL-AS-EXPECTED`. Qualquer FAIL ⇒ não ativar; ver passo 4.

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

## 7. Ativação futura por worker (NÃO implementada)
O driver atual (`src/drivers/docker-driver.ts` do NanoClaw) monta `--cap-drop=ALL` e
`--security-opt no-new-privileges` fixos (afirmados por `docker-driver.test.ts:89-90`) e **não expõe**
nenhuma `security_opt`/env para AppArmor ou seccomp. Portanto hoje não há como ligar isto por
worker. Passos futuros, cada um em PR próprio e **depois** de um spec em `docs/specs/`:
1. Spec: campo opcional por worker (ex. `securityProfile: "codex-bwrap"`), default = comportamento atual.
2. Driver: quando setado, acrescenta `--security-opt apparmor=argus-codex-worker` e
   `--security-opt seccomp=<caminho absoluto do JSON>`; falha cedo se o perfil não estiver carregado
   (`/sys/kernel/security/apparmor/profiles`) em vez de cair para o default. Nunca afrouxar
   `--cap-drop=ALL`/`no-new-privileges`.
3. Provisionamento (`install.sh`/`provision.sh`) instala o perfil com privilégio do operador, nunca o worker.
4. Ligar só ao worker Codex, depois do `ALL-AS-EXPECTED` do passo 3 na imagem que será usada.
