from fastapi import APIRouter

from app.routes.post_network_clustering import router as network_clustering_router
from app.routes.post_get_icps import router as get_icps_router
from app.routes.profile_fetching import router as get_profiles_from_icp
from app.routes.post_get_company_data import router as get_company_data_router
from app.routes.stream_news_fetching import router as stream_company_news
from app.routes.refine_icp import router as refine_icp
from app.routes.cache_linkedin_news import router as cache_linkedin_news
from app.routes.fetch_linkedin_news import router as fetch_linkedin_news
from app.routes.messages import router as messages_router
from app.routes.message_modifier import router as message_modifier_router
from app.routes.connection_rankings_delete import (
    router as connection_rankings_delete_router,
)
from app.routes.update_rank_connections import router as update_rank_connections_router
from app.routes.impact_ranking import router as impact_ranking_router
from app.routes.daily_email import router as daily_email_router
from app.routes.connection_priority import router as connection_priority_router
from app.routes.predictive_analysis import router as predictive_analysis_router

# Temporary Endpoints
from app.routes.get_company_news import router as get_company_news
from app.routes.stream_news_sse import router as stream_news_sse
from app.routes.news_scoring import router as news_scoring
from app.routes.keyword_tagline import router as keyword_tagline
from app.routes.fetch_news import router as fetch_news
from app.routes.me_summary import router as me_summary
from app.routes.me_assignment_chat import router as me_assignment_chat

router = APIRouter()

router.include_router(messages_router)
router.include_router(message_modifier_router)
router.include_router(connection_rankings_delete_router)
router.include_router(update_rank_connections_router)
router.include_router(impact_ranking_router)
router.include_router(daily_email_router)
router.include_router(connection_priority_router)
router.include_router(predictive_analysis_router)
router.include_router(network_clustering_router)
router.include_router(get_icps_router)
router.include_router(get_profiles_from_icp)
router.include_router(get_company_data_router)
router.include_router(stream_company_news)
router.include_router(refine_icp)
router.include_router(cache_linkedin_news)
router.include_router(fetch_linkedin_news)

# Temporary Non Streaming News Endpoint
router.include_router(get_company_news)
router.include_router(stream_news_sse)
router.include_router(news_scoring)
router.include_router(keyword_tagline)
router.include_router(fetch_news)
router.include_router(me_summary)
router.include_router(me_assignment_chat)
