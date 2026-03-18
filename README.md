# 🎯 Discord Username Checker

Uma ferramenta de alta performance desenvolvida em Python para verificação de disponibilidade de nomes de usuário no Discord. O projeto utiliza técnicas avançadas de rede e processamento paralelo para garantir velocidade e bypass de rate-limiting.

<img width="1109" height="625" alt="git" src="https://github.com/user-attachments/assets/41b5c11d-2084-449b-a9a3-f8ddeb035512" />

## 💡 Por que buscar nomes de usuários?

No ecossistema digital moderno, o **handle** (nome de usuário) tornou-se um ativo digital. Com a transição do Discord para o sistema de nomes únicos (sem as antigas tags #0000), a disputa por nomes curtos e estéticos cresceu exponencialmente.

* **Raridade:** Nomes com 2, 3 ou 4 caracteres são matematicamente limitados.
* **Estética:** Usuários e criadores de conteúdo buscam nomes "clean" para fortalecer sua identidade visual.
* **Valor de Mercado:** Existe um mercado secundário ativo para nomes curtos, considerados "OG" (Original Gangster).

## 🚀 Funcionalidades Técnicas

* **Multi-threading:** Processamento paralelo utilizando `ThreadPoolExecutor` para máxima eficiência.
* **Proxy Rotation:** Sistema de gerenciamento de proxies (HTTP/SOCKS5) com detecção automática de falhas e tempo de cooldown dinâmico.
* **Gerador Inteligente:** Algoritmos para gerar nomes baseados em padrões de raridade, sequências repetidas e caracteres específicos.
* **Notificações em Tempo Real:** Integração com Discord Webhooks para alertas imediatos quando um nome raro é encontrado.
* **Monitoramento de Recursos:** Sistema acoplado para monitorar uso de CPU e Memória RAM via `psutil`.

## 🛠️ Tecnologias Utilizadas

* **Linguagem:** Python 3.x
* **Bibliotecas:** * `Requests`: Comunicação com a API do Discord.
    * `Concurrent.futures`: Gerenciamento de threads.
    * `Colorama`: Interface de terminal estilizada.
    * `Psutil`: Monitoramento de performance do sistema.

## 📦 Como Instalar e Usar

1.  **Clone o repositório:**
    ```bash
    git clone [https://github.com/seu-usuario/discord-username-sentinel.git](https://github.com/seu-usuario/discord-username-sentinel.git)
    ```

2.  **Instale as dependências:**
    ```bash
    python -m pip install -r requirements.txt
    ```

3.  **Configure o `config.json`:**
    Substitua o campo `webhook_url` pelo seu link de webhook do Discord para receber alertas.

4.  **Adicione Proxies (Opcional):**
    Insira seus proxies no arquivo `proxy.txt` (um por linha) para evitar bloqueios de IP.

5.  **Execute o script:**
    ```bash
    python checker.py
    ```

---
*Este projeto foi desenvolvido para fins de estudo sobre automação de processos e integração de APIs. Use com responsabilidade.*
