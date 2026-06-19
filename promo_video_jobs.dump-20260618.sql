--
-- PostgreSQL database dump
--

\restrict duoA4hhbA1IwqWG3a80PkhFXQBiEALwzkF3k6aVTpJxXbWznACxf85qDCK2t39q

-- Dumped from database version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: promo_video_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.promo_video_jobs (
    id integer NOT NULL,
    bid_id integer,
    photo_id integer,
    price text,
    status text DEFAULT 'queued'::text NOT NULL,
    url text,
    error text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    kind text DEFAULT 'photo'::text NOT NULL,
    voice text,
    notify text,
    scenes integer DEFAULT 4 NOT NULL,
    script text,
    quality text DEFAULT 'fast'::text NOT NULL,
    progress text
);


ALTER TABLE public.promo_video_jobs OWNER TO postgres;

--
-- Name: promo_video_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.promo_video_jobs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.promo_video_jobs_id_seq OWNER TO postgres;

--
-- Name: promo_video_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.promo_video_jobs_id_seq OWNED BY public.promo_video_jobs.id;


--
-- Name: promo_video_jobs id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.promo_video_jobs ALTER COLUMN id SET DEFAULT nextval('public.promo_video_jobs_id_seq'::regclass);


--
-- Data for Name: promo_video_jobs; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.promo_video_jobs (id, bid_id, photo_id, price, status, url, error, created_at, updated_at, kind, voice, notify, scenes, script, quality, progress) FROM stdin;
1	2928	1752	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j1.html	\N	2026-06-12 11:48:49.171252	2026-06-12 12:00:27.06219	photo	\N	\N	4	\N	fast	\N
2	2928	\N	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j2.html	\N	2026-06-12 12:15:36.010181	2026-06-12 12:32:58.292879	spec	female	telegram	4	\N	fast	\N
3	2928	1752	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j3.html	\N	2026-06-12 13:06:41.972586	2026-06-12 13:40:34.477814	photo	female	telegram	6	\N	fast	\N
4	2928	\N	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j4.html	\N	2026-06-12 13:47:22.787544	2026-06-12 14:16:22.011961	spec	female	telegram	6	\N	fast	\N
5	2928	\N	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j5.html	\N	2026-06-12 14:17:39.681356	2026-06-12 14:33:46.925733	spec	female	telegram	6	\N	max	\N
6	2931	\N	$135,	cancelled	\N	operator cancelled - mistyped price	2026-06-12 14:29:43.662096	2026-06-12 14:33:46.990995	photo	\N	\N	4	\N	fast	\N
8	2928	\N	98000	done	https://experience-wholesale.net/static/uploads/promo/bid2928/j8.html	\N	2026-06-12 14:58:06.738213	2026-06-12 15:10:56.820078	spec	female	telegram	6	\N	max	\N
7	2925	\N	\N	done	https://experience-wholesale.net/static/uploads/promo/bid2925/j7.html	\N	2026-06-12 14:49:10.290261	2026-06-12 15:20:11.363402	photo	\N	telegram	4	Hi This is Jen with experience wholesale. today we have a 2024 Mercedes Benz GLE63 . The original msrp was over $136,000. Its silver on black with only 25,000 miles. In great shape, tires are good, both keys. 76000 at our place	max	\N
\.


--
-- Name: promo_video_jobs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.promo_video_jobs_id_seq', 8, true);


--
-- Name: promo_video_jobs promo_video_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.promo_video_jobs
    ADD CONSTRAINT promo_video_jobs_pkey PRIMARY KEY (id);


--
-- Name: promo_video_jobs promo_video_jobs_bid_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.promo_video_jobs
    ADD CONSTRAINT promo_video_jobs_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES public.bids(id) ON DELETE CASCADE;


--
-- Name: TABLE promo_video_jobs; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.promo_video_jobs TO expuser;


--
-- Name: SEQUENCE promo_video_jobs_id_seq; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE public.promo_video_jobs_id_seq TO expuser;


--
-- PostgreSQL database dump complete
--

\unrestrict duoA4hhbA1IwqWG3a80PkhFXQBiEALwzkF3k6aVTpJxXbWznACxf85qDCK2t39q

