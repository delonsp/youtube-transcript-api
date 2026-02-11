#!/usr/bin/env python3
"""
Wrapper para rodar estudos_avancados_processor em modo batch.

Lista videos pendentes e processa cada um automaticamente.
"""

import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    from estudos_avancados_processor import EstudosAvancadosManager

    manager = EstudosAvancadosManager()

    logger.info("Listando videos pendentes de Estudos Avancados...")
    pending = manager.list_pending_estudos_avancados()
    pending.sort(key=lambda v: v['published_at'])

    if not pending:
        logger.info("Nenhum video pendente.")
        return 0

    logger.info(f"{len(pending)} videos pendentes encontrados.")

    success = 0
    failed = 0

    for video in pending:
        video_id = video['video_id']
        title = video['title']
        logger.info(f"Processando: {title} ({video_id})")

        try:
            manager.process_video(video_id, dry_run=False)
            success += 1
            logger.info(f"OK: {video_id}")
        except Exception as e:
            failed += 1
            logger.error(f"FALHOU {video_id}: {e}")

    logger.info(f"Concluido: {success} ok, {failed} falhas")
    return 1 if failed > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
