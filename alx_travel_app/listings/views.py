from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .models import Listing, Review, Booking, Payment
from .serializers import (
    ListingSerializer, 
    ListingCreateSerializer, 
    ReviewSerializer,
    BookingSerializer,
    PaymentSerializer
)
import os
import requests
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404


class ListingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing travel listings.
    
    Provides CRUD operations for listings with filtering, searching, and ordering.
    """
    queryset = Listing.objects.filter(is_active=True)
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['listing_type', 'location']
    search_fields = ['title', 'description', 'location']
    ordering_fields = ['created_at', 'price', 'title']
    ordering = ['-created_at']
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'create':
            return ListingCreateSerializer
        return ListingSerializer
    
    def perform_create(self, serializer):
        """Set the creator when creating a listing"""
        serializer.save(created_by=self.request.user)
    
    @swagger_auto_schema(
        method='post',
        request_body=ReviewSerializer,
        responses={201: ReviewSerializer, 400: 'Bad Request'}
    )
    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def add_review(self, request, pk=None):
        """Add a review to a listing"""
        listing = self.get_object()
        serializer = ReviewSerializer(data=request.data)
        
        if serializer.is_valid():
            # Check if user already reviewed this listing
            if Review.objects.filter(listing=listing, reviewer=request.user).exists():
                return Response(
                    {'error': 'You have already reviewed this listing'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            serializer.save(listing=listing, reviewer=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @swagger_auto_schema(
        responses={200: ReviewSerializer(many=True)}
    )
    @action(detail=True, methods=['get'])
    def reviews(self, request, pk=None):
        """Get all reviews for a listing"""
        listing = self.get_object()
        reviews = listing.reviews.all()
        serializer = ReviewSerializer(reviews, many=True)
        return Response(serializer.data)


class ReviewViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing reviews.
    
    Users can only modify their own reviews.
    """
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['listing', 'rating']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter reviews based on user permissions"""
        if self.action in ['update', 'partial_update', 'destroy']:
            # Users can only modify their own reviews
            return Review.objects.filter(reviewer=self.request.user)
        return Review.objects.all()
    
    def perform_create(self, serializer):
        """Set the reviewer when creating a review"""
        serializer.save(reviewer=self.request.user)


class BookingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing bookings.
    
    Users can only view and modify their own bookings.
    """
    serializer_class = BookingSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'listing']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """Filter bookings to show only user's own bookings"""
        return Booking.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        """Set the user when creating a booking"""
        serializer.save(user=self.request.user)


class PaymentInitiateView(APIView):
    """Initiate payment with Chapa API."""
    def post(self, request, booking_id):
        booking = get_object_or_404(Booking, id=booking_id, user=request.user)
        chapa_key = os.environ.get('CHAPA_SECRET_KEY')
        if not chapa_key:
            return Response({'error': 'Chapa secret key not configured.'}, status=500)
        data = {
            "amount": str(booking.total_price),
            "currency": "ETB",
            "email": request.user.email,
            "first_name": request.user.first_name,
            "last_name": request.user.last_name,
            "tx_ref": f"booking_{booking.id}_{request.user.id}",
            "return_url": request.build_absolute_uri(f"/api/payments/verify/{booking.id}/"),
        }
        headers = {"Authorization": f"Bearer {chapa_key}"}
        response = requests.post("https://api.chapa.co/v1/transaction/initialize", json=data, headers=headers)
        if response.status_code == 200:
            resp_data = response.json()
            tx_id = resp_data['data']['tx_ref']
            payment, created = Payment.objects.get_or_create(
                booking=booking,
                defaults={
                    'amount': booking.total_price,
                    'transaction_id': tx_id,
                    'status': 'pending',
                }
            )
            return Response({
                'checkout_url': resp_data['data']['checkout_url'],
                'transaction_id': tx_id
            })
        return Response({'error': 'Failed to initiate payment.'}, status=400)


class PaymentVerifyView(APIView):
    """Verify payment status with Chapa API."""
    def get(self, request, booking_id):
        payment = get_object_or_404(Payment, booking_id=booking_id)
        chapa_key = os.environ.get('CHAPA_SECRET_KEY')
        if not chapa_key:
            return Response({'error': 'Chapa secret key not configured.'}, status=500)
        url = f"https://api.chapa.co/v1/transaction/verify/{payment.transaction_id}"
        headers = {"Authorization": f"Bearer {chapa_key}"}
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            resp_data = response.json()
            status_val = resp_data['data']['status']
            if status_val == 'success':
                payment.status = 'completed'
                payment.save()
                # TODO: trigger Celery task to send confirmation email
            elif status_val == 'failed':
                payment.status = 'failed'
                payment.save()
            return Response({'status': payment.status})
        return Response({'error': 'Failed to verify payment.'}, status=400)
