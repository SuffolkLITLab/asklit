"""Canonical navigation must keep OAuth state and callback on one origin."""
from fastapi.testclient import TestClient
from asklit.hosted.app import create_app


def test_primary_domain_navigation_and_api_compatibility(hosted):
    settings, store, runtime, *_ = hosted
    settings.base_url = 'https://asklit.suffolklitlab.org'
    settings.trust_fly_proxy = True
    with TestClient(create_app(settings, store, runtime), base_url='https://asklit-studio.fly.dev', follow_redirects=False) as client:
        response = client.get('/auth/google/login?next=%2Flit%2Fexample')
        assert response.status_code == 307
        assert response.headers['location'] == settings.base_url + '/auth/google/login?next=%2Flit%2Fexample'
        assert 'set-cookie' not in response.headers
        assert client.get('/health').status_code == 200
        assert client.get('/api/me').status_code == 401
        assert client.get('/privacy').headers['location'] == settings.base_url + '/privacy'
    with TestClient(create_app(settings, store, runtime), base_url=settings.base_url) as client:
        assert client.get('/').status_code == 200
        for route in ['/privacy', '/terms']:
            response = client.get(route)
            assert response.status_code == 200
            assert 'litlab@suffolk.edu' in response.text
            assert 'set-cookie' not in response.headers
