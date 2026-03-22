#!/usr/bin/env python3
"""
Cleanup stubs no Google Docs do Nível 1 (Tira Dúvidas).

Ações:
1. Deleta stubs de lives que não são Tira Dúvidas (Estudos Avançados, Saúde Integrativa, etc.)
2. Deleta stubs duplicados onde já existe entrada real na mesma data
3. Deleta stubs órfãos (sem video ID) quando há stub com video ID na mesma data
4. Preenche stubs legítimos de Tira Dúvidas com resumo via DeepSeek
"""

import os
import re
import pickle
import logging
import json
import time
from datetime import datetime
from typing import List, Dict, Optional
from collections import defaultdict

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from transcript_processor import TranscriptDownloader

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DOCUMENT_ID = '1wUM7wHVIK5C46Tqp30e-DqMSy1gqg5wt72Ppi701U4g'

# Títulos que NÃO pertencem ao doc do Nível 1
EXCLUDED_TITLE_PATTERNS = [
    r'estudos?\s+avan[cç]ad',
    r'sa[úu]de\s+integrativa',
    r'sa[úu]de\s+cerebral',
]

def get_deepseek_api_key():
    try:
        import keyring
        api_key = keyring.get_password('deepseek', 'api_key')
    except Exception:
        api_key = None
    return api_key or os.getenv('DEEPSEEK_API_KEY')


def is_excluded_title(title: str) -> bool:
    """Verifica se o título não pertence ao doc Nível 1."""
    title_lower = title.lower()
    for pattern in EXCLUDED_TITLE_PATTERNS:
        if re.search(pattern, title_lower):
            return True
    return False


class DocStubCleaner:
    def __init__(self):
        self.docs_service = None
        self.youtube_service = None
        self.transcript_downloader = TranscriptDownloader(
            captions_token_file='token_captions.pickle'
        )

    def authenticate(self):
        with open('token_docs.pickle', 'rb') as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        self.docs_service = build('docs', 'v1', credentials=creds)
        self.youtube_service = build('youtube', 'v3', credentials=creds)
        logger.info("Authenticated")

    def get_doc_sections(self) -> List[Dict]:
        """Parse document into sections based on HEADING_1 elements."""
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()
        content = doc.get('body', {}).get('content', [])

        # Find all HEADING_1 headers
        headers = []
        for elem in content:
            if 'paragraph' in elem:
                style = elem['paragraph'].get('paragraphStyle', {})
                if style.get('namedStyleType') == 'HEADING_1':
                    text = ''
                    link_url = ''
                    for run in elem['paragraph'].get('elements', []):
                        text += run.get('textRun', {}).get('content', '')
                        url = run.get('textRun', {}).get('textStyle', {}).get('link', {}).get('url', '')
                        if url:
                            link_url = url
                    headers.append({
                        'text': text.strip(),
                        'startIndex': elem.get('startIndex', 0),
                        'link_url': link_url,
                    })

        # Build sections with body text
        doc_end = content[-1].get('endIndex', 0)
        sections = []
        for i, header in enumerate(headers):
            end_index = headers[i + 1]['startIndex'] if i + 1 < len(headers) else doc_end

            body_text = ''
            for elem in content:
                start = elem.get('startIndex', 0)
                if start >= header['startIndex'] and start < end_index:
                    if 'paragraph' in elem:
                        for run in elem['paragraph'].get('elements', []):
                            body_text += run.get('textRun', {}).get('content', '')

            is_stub = 'preenchido manualmente' in body_text or ('[Pergunta]' in body_text and '[Resposta]' in body_text)
            video_ids = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', body_text)

            # Also check header link for video ID
            if header['link_url']:
                link_vid = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', header['link_url'])
                video_ids = list(set(video_ids + link_vid))

            sections.append({
                'date': header['text'],
                'startIndex': header['startIndex'],
                'endIndex': end_index,
                'is_stub': is_stub,
                'video_ids': video_ids,
                'body_preview': body_text[:200].replace('\n', ' '),
            })

        return sections

    def get_video_titles(self, video_ids: List[str]) -> Dict[str, str]:
        """Fetch video titles from YouTube API."""
        titles = {}
        unique_ids = list(set(video_ids))
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i:i+50]
            resp = self.youtube_service.videos().list(part='snippet', id=','.join(batch)).execute()
            for item in resp.get('items', []):
                titles[item['id']] = item['snippet']['title']
        return titles

    def classify_stubs(self, sections: List[Dict]) -> Dict:
        """Classify stubs into actions: delete or fill."""
        # Group by date
        by_date = defaultdict(list)
        for s in sections:
            by_date[s['date']].append(s)

        # Collect all video IDs from stubs to fetch titles
        all_stub_vids = []
        for s in sections:
            if s['is_stub'] and s['video_ids']:
                all_stub_vids.extend(s['video_ids'])

        video_titles = self.get_video_titles(all_stub_vids) if all_stub_vids else {}

        to_delete = []  # sections to delete
        to_fill = []    # sections to fill with content

        for date, items in by_date.items():
            stubs = [s for s in items if s['is_stub']]
            reals = [s for s in items if not s['is_stub']]

            if not stubs:
                continue

            # First pass: classify each stub
            excluded_stubs = []
            valid_stubs_with_vids = []
            orphan_stubs = []

            for stub in stubs:
                stub_title = None
                for vid in stub['video_ids']:
                    if vid in video_titles:
                        stub_title = video_titles[vid]
                        break

                if stub_title and is_excluded_title(stub_title):
                    excluded_stubs.append((stub, stub_title))
                elif stub['video_ids']:
                    valid_stubs_with_vids.append((stub, stub_title))
                else:
                    orphan_stubs.append(stub)

            # Delete excluded types
            for stub, title in excluded_stubs:
                to_delete.append({**stub, 'reason': f'Tipo errado: "{title}"'})

            # Delete orphans (no video ID)
            for stub in orphan_stubs:
                if reals or valid_stubs_with_vids or excluded_stubs:
                    to_delete.append({**stub, 'reason': f'Stub órfão (sem video ID)'})
                else:
                    to_delete.append({**stub, 'reason': f'Stub órfão sem video ID e sem contexto'})

            # Handle valid stubs with video IDs
            if reals:
                # Already have real entry → delete all valid stubs too
                for stub, title in valid_stubs_with_vids:
                    to_delete.append({**stub, 'reason': f'Já existe entrada real em {date}'})
            elif valid_stubs_with_vids:
                # Keep only the first one (most video IDs), delete the rest as duplicates
                # Sort by number of video IDs descending
                valid_stubs_with_vids.sort(key=lambda x: len(x[0]['video_ids']), reverse=True)
                best_stub, best_title = valid_stubs_with_vids[0]
                to_fill.append({**best_stub, 'title': best_title or 'Unknown'})
                for stub, title in valid_stubs_with_vids[1:]:
                    to_delete.append({**stub, 'reason': f'Duplicata de stub em {date}'})

        return {'delete': to_delete, 'fill': to_fill}

    def delete_sections(self, sections_to_delete: List[Dict]):
        """Delete sections from document one at a time, re-reading doc each time."""
        # We identify sections by their date text + video_ids to re-find them after shifts
        delete_specs = []
        for s in sections_to_delete:
            delete_specs.append({
                'date': s['date'],
                'video_ids': s['video_ids'],
                'reason': s['reason'],
                'has_versions': 'Versões:' in s.get('body_preview', ''),
            })

        deleted = 0
        for spec in delete_specs:
            # Re-parse doc to find current indices
            sections = self.get_doc_sections()
            target = None
            for s in sections:
                if not s['is_stub']:
                    continue
                if s['date'] != spec['date']:
                    continue
                # Match by video IDs if available
                if spec['video_ids'] and s['video_ids']:
                    if set(spec['video_ids']) & set(s['video_ids']):
                        target = s
                        break
                elif not spec['video_ids'] and not s['video_ids']:
                    target = s
                    break

            if not target:
                logger.warning(f"Could not re-find stub: {spec['date']} (vids: {spec['video_ids']})")
                continue

            # Get document end
            doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()
            doc_end = doc.get('body', {}).get('content', [])[-1].get('endIndex', 0)

            start = target['startIndex']
            end = target['endIndex']
            if end >= doc_end:
                end = doc_end - 1
            if start >= end:
                logger.warning(f"Skipping {spec['date']}: invalid range {start}-{end}")
                continue

            logger.info(f"Deleting: {spec['date']} (range {start}-{end})")
            logger.info(f"  Reason: {spec['reason']}")

            try:
                self.docs_service.documents().batchUpdate(
                    documentId=DOCUMENT_ID,
                    body={'requests': [{
                        'deleteContentRange': {
                            'range': {
                                'startIndex': start,
                                'endIndex': end,
                            }
                        }
                    }]}
                ).execute()
                deleted += 1
            except Exception as e:
                logger.error(f"  Failed to delete: {e}")

            time.sleep(0.5)

        return deleted

    def fill_stub(self, stub: Dict):
        """Replace a stub's content with actual summary."""
        logger.info(f"Filling: {stub['date']} - {stub.get('title', 'Unknown')}")

        # Download transcript
        transcript = None
        for vid in stub['video_ids']:
            try:
                result = self.transcript_downloader.download(vid, languages=['pt', 'pt-BR', 'en'])
                transcript = ' '.join([s['text'] for s in result['snippets']])
                if transcript:
                    break
            except Exception as e:
                logger.warning(f"  Failed to download {vid}: {e}")

        if not transcript:
            logger.error(f"  Could not download transcript for any video, skipping")
            return False

        # Generate summary
        summary_data = self._generate_summary(transcript, stub.get('title', stub['date']))
        if not summary_data:
            return False

        # Build replacement text
        qa_text = ""
        for qa in summary_data['qa_list']:
            qa_text += f"- Pergunta: {qa['pergunta']}\n  Resposta: {qa['resposta']}\n\n"

        sibling_urls = [f"https://youtube.com/watch?v={vid}" for vid in stub['video_ids']]
        siblings_note = ""
        if len(sibling_urls) > 1:
            siblings_note = f"\nVersões: {' | '.join(sibling_urls)}\n"

        new_body = f"""
<summary>
{summary_data['summary']}
</summary>
{siblings_note}
<qa_list>
{qa_text}</qa_list>

"""
        # We need to replace the body of the stub (everything between header end and section end)
        # Re-read the doc to get current indices (they may have shifted from deletions)
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()
        content = doc.get('body', {}).get('content', [])

        # Find this stub's header by matching date text and video IDs
        target_header_end = None
        target_section_end = None

        headers = []
        for elem in content:
            if 'paragraph' in elem:
                style = elem['paragraph'].get('paragraphStyle', {})
                if style.get('namedStyleType') == 'HEADING_1':
                    text = ''
                    for run in elem['paragraph'].get('elements', []):
                        text += run.get('textRun', {}).get('content', '')
                    headers.append({
                        'text': text.strip(),
                        'startIndex': elem.get('startIndex', 0),
                        'endIndex': elem.get('endIndex', 0),
                    })

        doc_end = content[-1].get('endIndex', 0)
        for i, h in enumerate(headers):
            if h['text'] == stub['date']:
                next_start = headers[i + 1]['startIndex'] if i + 1 < len(headers) else doc_end

                # Check if this section contains our video IDs
                section_text = ''
                for elem in content:
                    s = elem.get('startIndex', 0)
                    if s >= h['endIndex'] and s < next_start:
                        if 'paragraph' in elem:
                            for run in elem['paragraph'].get('elements', []):
                                section_text += run.get('textRun', {}).get('content', '')

                is_stub = 'preenchido manualmente' in section_text

                if is_stub:
                    target_header_end = h['endIndex']
                    target_section_end = next_start
                    break

        if target_header_end is None:
            logger.error(f"  Could not find stub in document after re-read")
            return False

        # Cannot delete past the end of the document
        doc_end = content[-1].get('endIndex', 0)
        if target_section_end >= doc_end:
            target_section_end = doc_end - 1

        # Delete old body, insert new, apply NORMAL_TEXT + Arial 11
        body_end_idx = target_header_end + len(new_body)
        self.docs_service.documents().batchUpdate(
            documentId=DOCUMENT_ID,
            body={'requests': [
                {
                    'deleteContentRange': {
                        'range': {
                            'startIndex': target_header_end,
                            'endIndex': target_section_end,
                        }
                    }
                },
                {
                    'insertText': {
                        'location': {'index': target_header_end},
                        'text': new_body,
                    }
                },
                {
                    'updateParagraphStyle': {
                        'range': {'startIndex': target_header_end, 'endIndex': body_end_idx},
                        'paragraphStyle': {'namedStyleType': 'NORMAL_TEXT'},
                        'fields': 'namedStyleType'
                    }
                },
                {
                    'updateTextStyle': {
                        'range': {'startIndex': target_header_end, 'endIndex': body_end_idx},
                        'textStyle': {
                            'fontSize': {'magnitude': 11, 'unit': 'PT'},
                            'weightedFontFamily': {'fontFamily': 'Arial'},
                        },
                        'fields': 'fontSize,weightedFontFamily'
                    }
                },
            ]}
        ).execute()

        # Add link to header if missing
        header_start = target_header_end - len(stub['date']) - 1
        if header_start >= 0 and stub['video_ids']:
            url = f"https://youtube.com/watch?v={stub['video_ids'][0]}"
            try:
                self.docs_service.documents().batchUpdate(
                    documentId=DOCUMENT_ID,
                    body={'requests': [{
                        'updateTextStyle': {
                            'range': {'startIndex': header_start, 'endIndex': header_start + len(stub['date'])},
                            'textStyle': {'link': {'url': url}},
                            'fields': 'link'
                        }
                    }]}
                ).execute()
            except Exception:
                pass

        logger.info(f"  Filled successfully!")
        return True

    def _generate_summary(self, transcript: str, title: str) -> Optional[Dict]:
        """Generate summary with DeepSeek."""
        from openai import OpenAI

        api_key = get_deepseek_api_key()
        if not api_key:
            logger.error("DeepSeek API key not found")
            return None

        prompt = f"""Analise a transcrição desta live "{title}" e gere:

1. Um RESUMO conciso (2-3 parágrafos) destacando os principais temas abordados.

2. Uma lista de PERGUNTAS E RESPOSTAS (5-10 itens) com as principais dúvidas respondidas na live.

Transcrição:
{transcript[:30000]}

Responda SOMENTE com JSON válido no formato:
{{
  "summary": "resumo aqui...",
  "qa_list": [
    {{"pergunta": "pergunta 1", "resposta": "resposta 1"}},
    {{"pergunta": "pergunta 2", "resposta": "resposta 2"}}
  ]
}}
"""
        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Você é um assistente que analisa transcrições e retorna JSON. Responda APENAS com JSON válido, sem texto adicional."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )
            result = json.loads(response.choices[0].message.content)
            return {
                'summary': result.get('summary', '[Resumo não gerado]'),
                'qa_list': result.get('qa_list', [])
            }
        except Exception as e:
            logger.error(f"DeepSeek error: {e}")
            return None

    def run(self, dry_run: bool = True, fill: bool = False, max_fill: int = 3):
        """Main cleanup routine."""
        print("📋 Analisando documento...\n")
        sections = self.get_doc_sections()
        classification = self.classify_stubs(sections)

        to_delete = classification['delete']
        to_fill = classification['fill']

        print(f"=== STUBS PARA DELETAR: {len(to_delete)} ===\n")
        for s in to_delete:
            print(f"  🗑️  {s['date']}")
            print(f"     Razão: {s['reason']}")
            print(f"     Range: {s['startIndex']}-{s['endIndex']}")
            print()

        print(f"=== STUBS PARA PREENCHER: {len(to_fill)} ===\n")
        for s in to_fill:
            print(f"  📝 {s['date']} — {s.get('title', 'Unknown')}")
            print(f"     Vids: {s['video_ids']}")
            print()

        if dry_run:
            print("⚠️  DRY RUN — nenhuma alteração feita.")
            print("   Use --execute para deletar stubs inválidos.")
            print("   Use --execute --fill para deletar E preencher.")
            return

        # Step 1: Delete invalid stubs
        if to_delete:
            print(f"\n🗑️  Deletando {len(to_delete)} stubs...\n")
            deleted = self.delete_sections(to_delete)
            print(f"✅ {deleted}/{len(to_delete)} stubs deletados.\n")

        # Step 2: Fill valid stubs (optional)
        if fill and to_fill:
            print(f"\n📝 Preenchendo {min(len(to_fill), max_fill)} stubs...\n")
            filled = 0
            for stub in to_fill[:max_fill]:
                success = self.fill_stub(stub)
                if success:
                    filled += 1
                time.sleep(2)
            print(f"\n✅ {filled}/{min(len(to_fill), max_fill)} stubs preenchidos.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Cleanup stubs no Google Docs Nível 1')
    parser.add_argument('--execute', action='store_true', help='Executar (sem isso, apenas dry run)')
    parser.add_argument('--fill', action='store_true', help='Também preencher stubs válidos (requer --execute)')
    parser.add_argument('--max-fill', type=int, default=3, help='Max stubs a preencher por execução (default: 3)')
    args = parser.parse_args()

    cleaner = DocStubCleaner()
    print("🔐 Autenticando...")
    cleaner.authenticate()
    cleaner.run(dry_run=not args.execute, fill=args.fill, max_fill=args.max_fill)


if __name__ == '__main__':
    main()
