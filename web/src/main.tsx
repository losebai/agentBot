import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { IndependentApp } from './IndependentApp';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode>{new URLSearchParams(location.search).has('standalone') ? <IndependentApp /> : <App />}</React.StrictMode>);
