# YouTube Admin na Magalu

Código canônico: delonsp/youtube-transcript-api, branch master. O nome da pasta
pode ser youtube-transcript-api; o serviço de produção chama-se youtube-admin.
A biblioteca upstream é apenas uma parte deste projeto; a automação cron também
pertence a este fork. Não depende do repositório plataforma-curso.

Produção: Compose próprio em /home/ubuntu/stack/youtube-admin, sem Dokploy.
Push, merge e pull NÃO fazem deploy. Não executar o Compose genérico da raiz
sobre produção: ele usa o serviço cron e volumes de desenvolvimento. O overlay
shutdown/youtube-admin.compose.json registra o projeto e os volumes externos
existentes; precisa da base privada de produção, não é uma stack completa.
Imagem fixada é local; não presumir que exista em registry. Rebuild não realizado
nesta reorganização. O Dockerfile da raiz passa a incluir o supervisor já aplicado.

O supervisor Linux drena jobs por45s, concede5s de SIGTERM e encerra resistentes;
init e stop_grace_period60s acompanham essa configuração. Capabilities efetivas:
CHOWN, SETUID e SETGID, com cap_drop ALL e no-new-privileges. Root e filesystem
gravável permanecem necessários ao cron atual. Horários usam BRT na produção.

Os arquivos supervisor, entrypoint e overlay foram transferidos sem alterar os
bytes de plataforma-curso@15823093. Aplicação anterior em14/09, hardening em23/09;
esta transferência não reinicia o serviço nem altera horários/OAuth/dados.
Procedimentos históricos e recuperação: repositório privado
https://github.com/delonsp/infra-magalu . Não repetir executores encerrados.

Testes sintéticos, sem APIs ou credenciais:
`python3 -m unittest discover -s ops_tests -v`.

No laptop, o fork costuma chamar-se origin-new (origin aponta ao jdepoix).
Verificar remotos e branch antes de atualizar. Na VPS, o remoto do fork é origin.
