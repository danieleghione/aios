

def test_a_repository_nobody_configured_is_not_reported_as_broken(environment):
    """An empty GitHub or manifest entry used to show ERROR, which reads as a
    defect of the appliance rather than something waiting for an operator."""
    import asyncio
    from aios import providers
    for provider, url in (('github', 'https://api.github.com'), ('internal', ''), ('http', '')):
        key = environment.uid()
        environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)',
                            (key, provider, provider, url, '{}'))
        result = asyncio.run(providers.sync(key))
        assert result['status'] == 'NOT CONFIGURED', (provider, result)
        assert 'Configure' in environment.one('SELECT error FROM repositories WHERE id=?', (key,))['error']
