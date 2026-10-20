# Proveniência e justificativa — políticas candidatas do worker Codex

Estado: **WIP — CANDIDATAS, não carregadas, não verificadas em runtime; sem aprovação de deploy** (sem sudo, sem `docker run`
nesta etapa). Compilam offline; nada aqui prova que o Bubblewrap funciona. A prova é o
[RUNBOOK](RUNBOOK.md). Mudança de comportamento exige spec antes de ativar (AGENTS.md):
[codex-sandbox-profile.spec.md](../../../docs/specs/codex-sandbox-profile.spec.md), também não implementada.

## Fontes primárias (consultadas em 2026-10-19)

| Fonte | Revisão | sha256 do arquivo |
|---|---|---|
| moby/profiles `apparmor/template.go` (perfil `docker-default`) | `4b8214fc4971e06172938a1f1554ccff6647ee80` (main) | `7da15d81aa5c507f5002b844fdc49a969904681447a2cc29222bd6cbe797163b` |
| moby/profiles `seccomp/default.json` | idem | `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6` |
| openai/codex `codex-rs/linux-sandbox/src/{bwrap,launcher,linux_run_main,landlock}.rs`, `cli/src/main.rs` | tag `rust-v0.162.1` → commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` | bwrap.rs `a6b97db5acaec475066f3a5338776f875f7f2033f008ee2532907a1e5e50ffc0`; linux_run_main.rs `3109246473ef2f8132c2d6555bec5b8116612ce58f28927608f322ac5c3fd8b1`; landlock.rs `4f92e49f44694d2c946ab2cb96fa4293daf52023b674a63332a863bb00c1370d` |
| containers/bubblewrap `bubblewrap.c`, `bind-mount.c` | `1fb0aa26d3eb66254f7742e704add7f82f41a582` (main; host tem 0.11.0) | bubblewrap.c `de141c805cb88f8faff9af1b3bb49098191fc67833b51b51fea84510ea4426ce`; bind-mount.c `13a01b3d13ee14ed305c6b09debd3784b52c8470a2ecbb4a9e3568792eb9cde6` |

Ressalvas: o Docker 29.5.2 instalado (`568f755`) usa o default embutido (`seccomp,profile=builtin`);
não foi provado que `moby/profiles@main` é byte a byte o vendorizado nele. O bubblewrap foi lido em
`main`, não em 0.11.0. Reconfirme ambos antes de ativar.

## Artefatos (sha256 no momento da escrita)

- `argus-codex-worker.apparmor` — `240001ea6abff5318dd3d3d5dba2e01b459c1b3e3fc72b05887fbe085ff78660`
- `argus-codex-worker.seccomp.json` — `d199b4c8cbe4106a1e5fadb078a37a15a4b56c49d1d28ca6922d94767c81d846` (formatado com Biome; o hash anterior, `f9739dde…`, era da saída crua do `jq`, semanticamente idêntica: mesmo JSON parseado)
- `scripts/derive-seccomp.sh` (derivação via `jq`; a saída crua precisa passar por `biome format --stdin-file-path=argus-codex-worker.seccomp.json` para bater byte a byte com o hash acima, exigência do `biome check .` do repositório), `scripts/validate-offline.sh`

## O que o Codex 0.162.1 realmente pede (leitura do fonte)

- `bwrap.rs`: `--unshare-user --unshare-ipc [--unshare-pid] [--unshare-net] --proc /proc --cap-drop ALL`,
  `--dev /dev`, `--ro-bind`, `--bind`, `--tmpfs`, `--remount-ro`.
- `linux_run_main.rs:422-445,1516`: se o mount de `/proc` falha (`Can't mount proc` … `Operation not
  permitted`/`Permission denied`), o Codex refaz sem `--proc`. **Por isso o perfil NÃO libera `mount
  proc`**: o `maskedPaths` do Docker o bloquearia de qualquer forma e há fallback.
- `landlock.rs`: o Codex aplica seu próprio seccomp (rede) + `PR_SET_NO_NEW_PRIVS`; `seccomp`/`prctl`
  já estão no default do Docker.
- `bubblewrap.c`: `clone(SIGCHLD|NEWNS|NEWUSER|NEWPID|NEWNET|NEWIPC[|NEWUTS|NEWCGROUP])` (l.3057-3115),
  `mount(NULL,"/",MS_SLAVE|MS_REC)` (l.3280 — é o `Failed to make / slave` observado), `mount tmpfs`
  (l.3284), bind `MS_BIND|MS_REC` (l.3303), `pivot_root` (l.3322,3366), `MS_REC|MS_PRIVATE` (l.3339),
  `umount2(MNT_DETACH)` (l.3342,3370); `bind-mount.c:513`: `MS_BIND|MS_REMOUNT`.

## Deltas vs. Docker default

### AppArmor (`docker-default` renderizado com abi 3.0, `tunables/global`, `abstractions/base`)
1. `deny mount,` removido; entra allow-list (mount não listado continua negado por padrão):
   `rslave -> /`, `tmpfs -> /**`, `bind`/`rbind /** -> /**`, `remount,bind -> /**`, `rprivate -> /**`,
   `devpts -> /**`.
2. `pivot_root,` adicionado.
3. Nada mais mudou: denies de famílias de socket, `/proc` e `/sys` escrita, `sysrq-trigger`, `kcore`,
   sinais e ptrace por peer. Sem `userns` rule e sem ABI 4: a prova local (docker-default AppArmor +
   seccomp aberto → `unshare -Ur` PASS) mostra que não é necessária, e o abi 3.0 mantém o
   comportamento já medido.

Status por regra: **`rslave -> /` é demonstrada necessária** (erro observado). As demais são
**candidatas deduzidas do fonte do bwrap, não verificadas**; os padrões de caminho `/**` são
deliberadamente largos porque o destino de bwrap é dinâmico (`/tmp/newroot/...`) e não foi medido.
O RUNBOOK passo 6 inclui como estreitá-los via `dmesg`/audit (`apparmor="DENIED" operation="mount"`).

### seccomp (default.json + 4 regras, `defaultAction` segue `SCMP_ACT_ERRNO`)
Todas com `excludes.caps: CAP_SYS_ADMIN` (quem tem a cap já era liberado pelo upstream):
- `unshare`: só se `flags & ~0x7e020000 == 0` (apenas flags de namespace).
- `clone` (e variante s390): só se `CLONE_NEWUSER` (0x10000000) está setado na mesma chamada.
- `mount`, `umount2`, `pivot_root`: sem condição de argumento possível (o alvo e o tipo não são
  filtráveis por BPF); o controle real é o kernel (cap só dentro do userns) + AppArmor.
- `clone3` continua `ENOSYS` (upstream); glibc/bwrap caem para `clone`. `setns`, `bpf`, `fsopen`,
  `move_mount`, `open_tree`, `sethostname` continuam só com `CAP_SYS_ADMIN`.

## Implicações e limites (sem prometer equivalência ao default)

- Qualquer processo do worker, não só o bwrap, passa a poder criar user+mount namespaces e montar
  tmpfs/bind/devpts dentro deles. Aumenta a superfície do kernel (userns é historicamente fonte de
  CVEs de LPE). O default do Docker negava isso por inteiro.
- Dentro do userns o processo tem caps *naquele* userns; `no-new-privileges` e o bounding set vazio
  do container permanecem. Não vira root no host, e sem rede (`--network none` no probe).
- `tmpfs -> /**` permite tmpfs sobre qualquer caminho **da própria mount ns**; bind `/** -> /**` permite
  rebind de qualquer coisa já visível ao container. Em aberto: se isso permite desfazer os
  maskedPaths do Docker (ex.: bind de um diretório vazio é inofensivo, mas expor `/proc/kcore` não
  deve ser possível porque o AppArmor ainda nega `kcore`). **Não verificado**: ver `mask-bypass` no RUNBOOK.
- Host `restrict_unprivileged_userns=1`: perfis não-`unconfined` (como este) não são o alvo da
  restrição, coerente com a prova local; um upgrade do Ubuntu pode mudar isso.
- Perfil nomeado `argus-codex-worker` precisa ser carregado no host antes de qualquer `docker run`
  que o use; sem ele o Docker falha ao criar o container (fail closed).
