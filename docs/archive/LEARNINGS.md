# Learnings — Notas pedagógicas do projeto Peel

## Passo 4 — Pitchfork RSS

### Reparo 1: Código morto em `_slugify()`

**Localização:** `src/peel/sources/rss.py`, método `_slugify()`

**Problema:**
```python
s = re.sub(r'["\'\'\"]', "", s)     # ← MORTO: só remove " e '
s = re.sub(r"[^a-z0-9]+", "-", s)   # ← Esta já trata tudo
```

**Análise:**
- A primeira regex `["\'\'\"]` tem caracteres redundantes: `\'` é só `'` escapado.
- Apenas remove 2 caracteres distintos: `"` e `'`.
- Mas a segunda linha `[^a-z0-9]+` já substitui *qualquer* caractere não-alfanumérico por `-`.
- A primeira linha foi escrita a pensar em aspas curly (U+201C/U+201D) mas não as incluiu.
- **A linha pode ser apagada sem nada partir.**

**Lição:** Ter cuidado com regexes com caracteres repetidos e com pensamento incompleto (queria curly quotes mas não as adicionou).

---

### Reparo 2: Bug latente com apóstrofo curly (U+2019)

**Localização:** `src/peel/sources/rss.py`, método `_slugify()` e `_extract_artist_title()`

**Problema:**
```
Input: "It's Working"  (apóstrofo curly U+2019)
Output slug: "it-s-working"  ← Espaço onde deveria ser nada
```

**Causa:**
- `_extract_artist_title()` remove aspas retas e curly: `r'^["\'\u201c\u201d]|["\'\u201c\u201d]$'`
- Mas não remove o apóstrofo curly U+2019 (`'`)
- O apóstrofo sobrevive até `_slugify()` → `[^a-z0-9]+` vê `'` como não-alfanumérico → `-`

**Solução (não implementada):**
Adicionar U+2019 à regex de quote-stripping:
```python
title = re.sub(r'^["\'\u201c\u201d\u2019]|["\'\u201c\u201d\u2019]$', "", title).strip()
```

Ou melhor: remover `["\'\u201c\u201d\u2019]` *em qualquer posição*, não só início/fim:
```python
title = re.sub(r'[\"\'\u201c\u201d\u2019]', "", title).strip()
```

**Lição:** Unicode é tricky. Alguns caracteres parece-se (quote reto vs curly vs apóstrofo) mas são U+XXXX distintos. Ferramentas como `unicodedata.category()` ajudam.

---

## Padrão: edit vs write

**Haiku falha a escapar regex em JSON:**

| Caso | Tool | Razão |
|------|------|-------|
| Ficheiro com **regex** (`\s`, `\(`, etc.) | `write` | Ficheiro inteiro é um campo — escaping robusto |
| Ficheiro com **aspas especiais** | `write` | Menos necessidade de escape |
| Ficheiro **lógica pura** | `edit` | Funciona bem, incremental |
| **Dúvida** | `write` | Zero risco |

**Exemplo:** `rss.py` é 80% regex → use `write`. Testes simples → `edit` é OK.

---

## Para implementar depois (v2+)

- [ ] Curly apostrophe (U+2019) em `_extract_artist_title()`
- [ ] Limpar código morto em `_slugify()`
- [ ] Adicionar mais fontes RSS (Stereogum, KEXP, NTS)
- [ ] Spotify playlist sources (BBC 6 Music, Gilles Peterson)
