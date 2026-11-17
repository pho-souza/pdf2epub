# pdf2epub

Converte PDFs (artigos, relatórios) em EPUB para leitura em e-readers, com foco no **Xteink X4** (480x800, e-ink).
Tabelas, imagens e gráficos vetoriais são detectados e reinseridos no texto como imagem.

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
.venv/bin/python pdf2epub.py artigo.pdf                       # Xteink X4 (padrão) -> artigo.epub
.venv/bin/python pdf2epub.py artigo.pdf -o saida.epub
.venv/bin/python pdf2epub.py artigo.pdf --device normal       # Kindle e similares, cores originais
.venv/bin/python pdf2epub.py artigo.pdf --footnotes linked    # notas de rodapé com links
.venv/bin/python pdf2epub.py *.pdf                            # vários arquivos de uma vez
```

## Presets (parameters.json)

Os parâmetros podem ser carregados de [parameters.json](parameters.json) (ao lado do script), que traz os presets
`default`, `xteink` e `kindle`:

```bash
.venv/bin/python pdf2epub.py artigo.pdf --preset kindle
.venv/bin/python pdf2epub.py artigo.pdf --preset xteink --footnotes linked   # a linha de comando sobrepõe o preset
.venv/bin/python pdf2epub.py artigo.pdf --params meus_presets.json --preset leitura
```

Precedência: valores internos < preset `default` do arquivo < preset escolhido < opções da linha de comando.
Sem `--preset`, vale o `default` do arquivo (se o arquivo não existir, valem os padrões internos).
Para criar um preset novo, copie um bloco no JSON e mude o nome. Chaves aceitas: `device`, `footnotes`, `formulas`,
`width`, `margin`, `dpi`, `color`, `chart_margin`, `min_fig`, `text_tables`, `lang` (`null` = usa o padrão do `device`).
Para booleanos na linha de comando use `--color/--no-color` e `--text-tables/--no-text-tables`.

## Opções

| Opção | Descrição |
|---|---|
| `--device xteink` (padrão) | Imagens em tons de cinza, largura 480 px, 220 dpi |
| `--device normal` | Imagens coloridas (cores originais), largura 1072 px, 200 dpi |
| `--footnotes end` (padrão) | Notas listadas em "Notas" no fim do capítulo |
| `--footnotes linked` | O número no texto vira link para a nota (e a nota tem link de volta). Notas são associadas pela página em que aparecem e marcadas com `epub:type="noteref"/"footnote"` (pop-up em leitores compatíveis) |
| `--formulas image` (padrão) | Equações em destaque (blocos com fonte/símbolos matemáticos, incluindo frações, índices e número da equação) viram imagem |
| `--formulas text` | Mantém as equações como texto |
| `--preset`, `--params` | Carrega um preset de `parameters.json` (ou de outro JSON) |
| `--color / --no-color` | Imagens coloridas ou em tons de cinza (sobrescreve o perfil) |
| `--width`, `--margin`, `--dpi` | Sobrescrevem os valores do perfil |
| `--chart-margin` | Distância máxima (pt) para incluir rótulos e títulos de eixo e legendas no recorte do gráfico (padrão 21; texto girado também é incluído) |
| `--min-fig` | Tamanho mínimo (pt) para considerar uma região como figura (padrão 40) |
| `--text-tables` | Detecta também tabelas sem bordas (heurístico, pode gerar falsos positivos) |
| `--lang` | Idioma do EPUB (padrão `pt`) |
| `-o, --output` | Arquivo de saída (apenas com um PDF) |

## O que é feito

- Texto reflowable com títulos (h1–h3, por tamanho de fonte), negrito e itálico.
- Remoção de cabeçalhos/rodapés repetidos e números de página.
- Hifenização de fim de linha desfeita; parágrafos cortados entre páginas/colunas são unidos; ligaduras normalizadas.
- Leitura em duas colunas (heurística).
- Capítulos divididos pelos títulos, com sumário (nav + NCX).
- Equações em destaque viram imagem.
- Legendas ("Figura 1", "Tabela 2", "Gráfico 3") ficam como texto junto à imagem.

## Limitações

- PDFs escaneados (só imagem) precisariam de OCR; não incluído.
- Fórmulas: só equações em destaque (em bloco próprio) são detectadas; fórmulas inline no meio de uma frase continuam como texto. A detecção depende de fonte matemática (LaTeX/Cambria Math/Symbol) ou de muitos símbolos; fórmulas em PDFs que as embutem como imagem já saem como figura.
- Gráficos vetoriais muito fragmentados podem ser recortados em partes.
- As heurísticas (títulos, notas de rodapé, colunas) foram ajustadas com poucos PDFs; podem falhar em outros layouts.
